# -*- coding: utf-8 -*-
"""
交叉编码精排（Cross-Encoder Reranker）—— RAG 检索的第二阶段。

背景
----
混合检索（BM25 + 向量 RRF）已拿到 recall@3 = 100%，但 top-1 只有 75%。
精排的目标：在召回池（top-K，K=10~15）里，用 query-chunk 配对评分把最相关的 chunk 推到 top-1。

为什么不用 BGE-Reranker
------------------------
本机 venv 创建失败、重依赖必翻车（Day3 教训）。
改用 DeepSeek API 做交叉编码：pairwise scoring，每条 query×chunk 一次调用，
成本约 ¥0.0003/条，20 条语料跑完一次精排总成本 < ¥0.01。

实现要点
--------
1. **pairwise scoring**：每对 (query, chunk) 独立打分，不共享上下文，避免 token 爆炸
2. **batch 接口复用**：直接调 DeepSeekProvider.raw_call()，temperature=0 保证确定性
3. **零新增依赖**：沿用 llm_fallback 的 provider 封装
4. **向下兼容**：无 API Key 时返回原始顺序，不崩溃

评测指标新增
------------
- P@1 = top-1 命中的比例（原 recall@k 只测「是否在 top-k 里」，不区分排名）
- NDCG@K = 考虑排名的质量指标（比 recall 更能反映精排效果）
"""

import argparse
import json
import math
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from llm_fallback import FallbackManager, DeepSeekProvider
except Exception:
    FallbackManager = None
    DeepSeekProvider = None

from vector_retriever import build_hybrid, HybridRetriever
from retriever import load_corpus, CORPUS_PATH

# ----------------------------------------------------------------------
# 可调常量
# ----------------------------------------------------------------------
DEFAULT_TOP_K_CANDIDATES = 10   # 精排候选池大小（从混合检索取 top-K）
DEFAULT_SCORE_THRESHOLD = 0.0   # 低于此分数的候选被丢弃


# ----------------------------------------------------------------------
# Prompt 模板
# ----------------------------------------------------------------------
JUDGE_SYSTEM_PROMPT = """你是一个专业的检索相关性评分员。你的任务是为「查询」与「文档片段」的相关性打分。

打分标准（1-5 分制）：
- 5 分：片段完美回答查询，包含所有关键信息
- 4 分：片段高度相关，包含大部分关键信息
- 3 分：片段部分相关，包含部分关键信息
- 2 分：片段关联度低，仅有少量相关信息
- 1 分：片段与查询无关或错误信息

输出 JSON（不要有其他文字）：
{"score": 数字, "reason": "一句话理由"}"""

JUDGE_USER_TEMPLATE = """查询：{query}
文档片段：{chunk_text}

请判断该片段对查询的相关性，输出 JSON。"""


# ----------------------------------------------------------------------
# 核心类
# ----------------------------------------------------------------------
class LLMReranker:
    """基于 DeepSeek 的交叉编码精排器。

    用法：
        reranker = LLMReranker()
        ranked = reranker.rerank(query, candidates, top_k=5)
    """

    def __init__(self, api_key=None, timeout=10.0, provider=None, verbose=True):
        self.verbose = verbose
        self.score_count = 0
        self.fallback_count = 0
        self.total_cost = 0.0

        if provider is not None:
            self.provider = provider
        elif DeepSeekProvider is None:
            self.provider = None
        else:
            key = api_key or self._load_api_key()
            self.provider = DeepSeekProvider(key, timeout=timeout) if key else None

        if self.provider is None:
            print("[警告] 无 DeepSeek API Key，精排将返回原始顺序")

    @staticmethod
    def _load_api_key():
        try:
            if FallbackManager:
                cfg = FallbackManager._load_config()
                return cfg.get("deepseek", {}).get("api_key")
        except Exception:
            pass
        return os.environ.get("DEEPSEEK_API_KEY")

    def rerank(self, query, candidates, top_k=5):
        """精排候选列表。

        参数
            query      : 用户查询
            candidates : list[dict]，每条含 id/text/score（来自混合检索）
            top_k      : 返回 top-k

        返回 list[dict]，按相关性分数降序排列
        """
        if not candidates:
            return []

        # 逐条打分
        scored = []
        for i, cand in enumerate(candidates):
            chunk_text = cand.get("text", "")
            prompt = JUDGE_USER_TEMPLATE.format(query=query, chunk_text=chunk_text)

            try:
                raw = self.provider.raw_call(JUDGE_SYSTEM_PROMPT, prompt)
                data = self._parse_score(raw)
                score = data.get("score", 3)
                reason = data.get("reason", "")
                self.score_count += 1
            except Exception as e:
                if self.verbose:
                    print(f"  [精排失败] chunk {cand.get('id')}: {e}")
                score = 3.0  # 默认中等分
                reason = f"error: {e}"
                self.fallback_count += 1

            scored.append({
                **cand,
                "rerank_score": score,
                "rerank_reason": reason,
            })

        # 按分数降序排列
        ranked = sorted(scored, key=lambda x: x["rerank_score"], reverse=True)

        # 截断到 top_k
        return ranked[:top_k]

    @staticmethod
    def _parse_score(raw):
        """解析精排返回的 JSON。"""
        s = raw.strip()
        # 剥代码块
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s)
        if m:
            s = m.group(1).strip()
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end > start:
            s = s[start:end + 1]
        return json.loads(s)

    def summary(self):
        return {
            "scored_pairs": self.score_count,
            "fallbacks": self.fallback_count,
            "est_cost_yuan": round(self.total_cost, 6),
        }


# ----------------------------------------------------------------------
# 辅助：计算 P@1 和 NDCG@K
# ----------------------------------------------------------------------
def calc_P_at_K(ranked_hits, expects, K):
    """Precision@K：top-K 中命中的比例。"""
    top_k = ranked_hits[:K]
    hit_ids = {h["id"] for h in top_k}
    expected = set(expects)
    hits = hit_ids & expected
    return len(hits) / K if K > 0 else 0.0


def calc_recall_at_K(ranked_hits, expects, K):
    """Recall@K：top-K 中覆盖了多少期望 chunk。"""
    top_k = ranked_hits[:K]
    hit_ids = {h["id"] for h in top_k}
    expected = set(expects)
    covered = hit_ids & expected
    return len(covered) / len(expected) if expected else 0.0


def calc_NDCG_at_K(ranked_hits, expects, K):
    """NDCG@K：考虑排名的归一化折损累积增益。"""
    top_k = ranked_hits[:K]
    expected = set(expects)

    # DCG
    dcg = 0.0
    for i, h in enumerate(top_k):
        if h["id"] in expected:
            dcg += 1.0 / math.log2(i + 2)  # i+2 因为 i 从 0 开始

    # IDCG（理想排序）
    n_relevant = min(len(expected), K)
    idcg = sum(1.0 / math.log2(i + 2) for i in range(n_relevant))

    return dcg / idcg if idcg > 0 else 0.0


import math


# ----------------------------------------------------------------------
# Demo / CLI
# ----------------------------------------------------------------------
DEMO_QUERIES = [
    ("Q01", "哪些客户问过 LED Downlight？", ["C04", "C06"]),
    ("Q02", "LED Panel Light PL-6060 的报价历史", ["C01", "C02", "C03"]),
    ("Q10", "有没有客户投诉过质量问题？", ["C04", "C08"]),
    ("Q12", "哪些客户可以谈更长的回款时间？", ["C10"]),
]


def main():
    ap = argparse.ArgumentParser(description="RAG 精排：Cross-Encoder 重排序 + P@1/NDCG")
    ap.add_argument("--top-k-candidates", type=int, default=DEFAULT_TOP_K_CANDIDATES,
                    help="精排候选池大小（默认 10）")
    ap.add_argument("--top-k-output", type=int, default=5,
                    help="精排后输出条数（默认 5）")
    ap.add_argument("--no-rerank", action="store_true", help="跳过精排，直接对比原始排名")
    args = ap.parse_args()

    # 加载
    retr, chunks = build_hybrid()
    reranker = LLMReranker()

    print(f"\n🔍 RAG 精排评测 | 候选池 top-{args.top_k_candidates} | 输出 top-{args.top_k_output}")
    print(f"语料: {len(chunks)} 条\n")

    results = []
    for qid, query, expects in DEMO_QUERIES:
        print(f"{'='*60}")
        print(f"【{qid}】{query}")
        print(f"期望命中: {expects}")
        print("-" * 60)

        # 1) 原始混合检索
        t0 = time.time()
        original = retr.search(query, top_k=args.top_k_candidates, return_scores=True)
        original_ids = [h["id"] for h in original]
        elapsed_orig = time.time() - t0

        # 2) 精排
        if not args.no_rerank:
            t0 = time.time()
            reranked = reranker.rerank(query, original, top_k=args.top_k_output)
            reranked_ids = [h["id"] for h in reranked]
            elapsed_rerank = time.time() - t0
        else:
            reranked = original[:args.top_k_output]
            reranked_ids = original_ids[:args.top_k_output]
            elapsed_rerank = 0

        # 3) 计算指标
        p1_orig = calc_P_at_K(original, expects, 1)
        p1_rerank = calc_P_at_K(reranked, expects, 1)
        r3_orig = calc_recall_at_K(original, expects, 3)
        r3_rerank = calc_recall_at_K(reranked, expects, 3)
        ndcg3_orig = calc_NDCG_at_K(original, expects, 3)
        ndcg3_rerank = calc_NDCG_at_K(reranked, expects, 3)

        print(f"\n【原始排名 top-{args.top_k_output}】")
        print(f"  IDs: {original_ids[:args.top_k_output]}")
        print(f"  P@1={p1_orig:.0%}  R@3={r3_orig:.0%}  NDCG@3={ndcg3_orig:.3f}")

        print(f"\n【精排后 top-{args.top_k_output}】")
        print(f"  IDs: {reranked_ids}")
        print(f"  P@1={p1_rerank:.0%}  R@3={r3_rerank:.0%}  NDCG@3={ndcg3_rerank:.3f}")

        if p1_rerank > p1_orig:
            print(f"  ✅ P@1 提升：{p1_orig:.0%} → {p1_rerank:.0%}")
        elif p1_rerank == p1_orig and p1_orig == 1.0:
            print(f"  ✓ P@1 保持 100%")

        # 打印精排分数
        print(f"\n【精排打分明细】")
        for i, h in enumerate(reranked[:args.top_k_output], 1):
            marker = " ★" if h["id"] in expects else ""
            print(f"  {i}. [{h['id']}] score={h.get('rerank_score', '?')} {marker}")
            if h.get("rerank_reason"):
                print(f"     → {h['rerank_reason'][:60]}")

        results.append({
            "qid": qid,
            "query": query,
            "expects": expects,
            "p1_original": p1_orig,
            "p1_reranked": p1_rerank,
            "r3_original": r3_orig,
            "r3_reranked": r3_rerank,
            "ndcg3_original": ndcg3_orig,
            "ndcg3_reranked": ndcg3_rerank,
        })

    # 汇总
    print(f"\n{'='*60}")
    print("📊 精排效果汇总")
    print(f"{'='*60}")
    avg_p1_orig = sum(r["p1_original"] for r in results) / len(results)
    avg_p1_rerank = sum(r["p1_reranked"] for r in results) / len(results)
    avg_ndcg3_orig = sum(r["ndcg3_original"] for r in results) / len(results)
    avg_ndcg3_rerank = sum(r["ndcg3_reranked"] for r in results) / len(results)
    print(f"P@1 原始: {avg_p1_orig:.1%} | 精排: {avg_p1_rerank:.1%}")
    print(f"NDCG@3 原始: {avg_ndcg3_orig:.3f} | 精排: {avg_ndcg3_rerank:.3f}")
    print(f"精排调用: {reranker.score_count} 次 | 降级: {reranker.fallback_count} 次")
    print(f"总耗时: {elapsed_orig + elapsed_rerank:.2f}s")
    print()


if __name__ == "__main__":
    main()
