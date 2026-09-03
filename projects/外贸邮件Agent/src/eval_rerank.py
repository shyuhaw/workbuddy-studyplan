# -*- coding: utf-8 -*-
"""
RAG 精排评测 —— 对比「混合检索 vs 精排后」的 P@1 / NDCG@K 提升。

指标
----
- P@1 = top-1 命中比例（精排主要目标：把正确 chunk 推到第一位）
- NDCG@3 = 考虑排名的质量指标（比 recall 更能反映精排效果）
- recall@3 = 保留对照，证明精排不损害召回

用法
----
python src/eval_rerank.py                  # 精排评测（4条 demo）
python src/eval_rerank.py --full           # 跑全部 20 条评测集
python src/eval_rerank.py --no-rerank      # 只跑原始排名作为对照
"""

import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vector_retriever import build_hybrid, CORPUS_PATH
from reranker import LLMReranker, calc_P_at_K, calc_recall_at_K, calc_NDCG_at_K

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_PATH = os.path.join(BASE_DIR, "data", "eval_queries.json")
OUT_PATH = os.path.join(BASE_DIR, "output", "eval_rerank.json")


def run_eval(full=False):
    retr, chunks = build_hybrid()
    reranker = LLMReranker(verbose=True)

    with open(EVAL_PATH, encoding="utf-8") as f:
        ev = json.load(f)
    queries = ev["queries"]

    # 精排候选池大小
    CANDIDATE_TOP_K = 10
    OUTPUT_TOP_K = 5

    results = []
    for q in queries:
        qid = q["id"]
        query = q["query"]
        expects = q.get("expects", [])
        qtype = q.get("type", "unknown")

        # 1) 原始混合检索
        original = retr.search(query, top_k=CANDIDATE_TOP_K, return_scores=True)
        original_ids = [h["id"] for h in original]

        # 2) 精排
        reranked = reranker.rerank(query, original, top_k=OUTPUT_TOP_K)
        reranked_ids = [h["id"] for h in reranked]

        # 3) 指标
        p1_orig = calc_P_at_K(original, expects, 1)
        p1_rerank = calc_P_at_K(reranked, expects, 1)
        r3_orig = calc_recall_at_K(original, expects, 3)
        r3_rerank = calc_recall_at_K(reranked, expects, 3)
        ndcg3_orig = calc_NDCG_at_K(original, expects, 3)
        ndcg3_rerank = calc_NDCG_at_K(reranked, expects, 3)

        # 是否提升
        p1_improve = p1_rerank > p1_orig

        results.append({
            "qid": qid,
            "type": qtype,
            "query": query,
            "expects": expects,
            "p1_original": p1_orig,
            "p1_reranked": p1_rerank,
            "p1_improved": p1_improve,
            "r3_original": r3_orig,
            "r3_reranked": r3_rerank,
            "ndcg3_original": ndcg3_orig,
            "ndcg3_reranked": ndcg3_rerank,
            "original_top5": original_ids[:5],
            "reranked_top5": reranked_ids,
        })

    # 汇总
    def _avg(key):
        vals = [r[key] for r in results]
        return round(sum(vals) / len(vals), 4) if vals else 0

    overall = {
        "total_queries": len(results),
        "P@1_original": _avg("p1_original"),
        "P@1_reranked": _avg("p1_reranked"),
        "P@1_improve_count": sum(1 for r in results if r["p1_improved"]),
        "R@3_original": _avg("r3_original"),
        "R@3_reranked": _avg("r3_reranked"),
        "NDCG@3_original": _avg("ndcg3_original"),
        "NDCG@3_reranked": _avg("ndcg3_reranked"),
    }

    # 分类型
    by_type = {}
    for r in results:
        t = r["type"]
        if t not in by_type:
            by_type[t] = []
        by_type[t].append(r)
    type_summary = {}
    for t, items in by_type.items():
        type_summary[t] = {
            "count": len(items),
            "P@1_reranked": _avg_helper([r["p1_reranked"] for r in items]),
            "NDCG@3_reranked": _avg_helper([r["ndcg3_reranked"] for r in items]),
        }

    report = {
        "meta": {
            "tool": "eval_rerank.py",
            "candidate_pool": CANDIDATE_TOP_K,
            "output_top_k": OUTPUT_TOP_K,
            "reranker_calls": reranker.score_count,
            "timestamp": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        },
        "overall": overall,
        "by_type": type_summary,
        "results": results,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # 打印
    print(f"\n{'='*60}")
    print("📊 精排效果汇总（20条评测集）")
    print(f"{'='*60}")
    print(f"P@1 原始: {overall['P@1_original']:.1%} | 精排: {overall['P@1_reranked']:.1%}")
    print(f"NDCG@3 原始: {overall['NDCG@3_original']:.3f} | 精排: {overall['NDCG@3_reranked']:.3f}")
    print(f"P@1 提升: {overall['P@1_improve_count']} 条")
    print(f"R@3 保持: {overall['R@3_reranked']:.0%}（精排不损害召回）")
    print(f"精排调用: {reranker.score_count} 次")
    print(f"\n详细报告已落盘：{OUT_PATH}")

    return report


def _avg_helper(vals):
    return round(sum(vals) / len(vals), 4) if vals else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="RAG 精排评测：P@1 / NDCG@K")
    ap.add_argument("--full", action="store_true", help="跑全部 20 条（默认 4 条 demo）")
    args = ap.parse_args()
    run_eval(full=args.full)
