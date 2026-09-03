# -*- coding: utf-8 -*-
"""
生成端（RAG 的 G）—— 把「检索到的历史」真正变成「带引用的答案」。

为什么必须有这个文件：
    在此之前 workflow.py::_render_draft() 是纯 f-string 模板拼接，
    全项目没有任何 LLM 起草路径 —— 检索到的历史只是被塞进字符串，不是生成。
    这个文件让 RAG 第一次真正闭合：检索 → 组装 → 生成 → 可评测。

核心设计
--------
1. **强制引用标注**：答案里每句话末尾必须带 [n]，同时要求模型返回 JSON 的 cited 数组。
   - 内联 [n]  → 给人看（可读、可核对）
   - cited 数组 → 给机器看（可程序化验证）
   两者取并集，任一路缺失都不算完全失败，但都会被记录。

2. **复用已有封装**：直接调 llm_fallback.DeepSeekProvider.raw_call()，不重写 HTTP。
   沿用其 temperature=0（确定性，抑制幻觉）与 JSON 输出模式。

3. **优雅降级（这是项目一贯风格）**：
   调用异常 / 超时 / JSON 解析失败 → 回退模板，标记 mode=template_fallback，不抛异常。

4. **不掩盖问题**：答案一个引用都没有时**不回退模板**（模板同样没有引用，回退只是掩盖），
   而是打上 no_citation 标记，交给评测层计为失败项。

零新增依赖（requests 已是项目既有依赖）。
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from context_builder import (  # noqa: E402
    EMPTY_CONTEXT, extract_citations, map_citations, est_tokens,
    _CHUNK_CACHE,
)

try:
    from llm_fallback import DeepSeekProvider, LOG_FILE
except Exception:  # 兜底：LLM 层不可用时仍可导入本模块做模板降级
    DeepSeekProvider = None
    LOG_FILE = None


# ----------------------------------------------------------------------
# 可调常量
# ----------------------------------------------------------------------
DEFAULT_TIMEOUT = 5.0        # 生成端超时（秒）。超过即降级，不能被单次调用拖死整条流水线
MAX_CONTEXT_CHARS = 2000
# DeepSeek 官方计价（2026-09）：输入 1 元/百万 token，输出 8 元/百万 token（缓存未命中）
PRICE_IN_PER_TOKEN = 1.0 / 1_000_000
PRICE_OUT_PER_TOKEN = 8.0 / 1_000_000

SYSTEM_PROMPT = """你是一个外贸业务助手，只能依据给定的「历史往来片段」回答问题。

严格规则：
1. **每句话末尾必须标注引用编号**，格式为 [n] 或 [n][m]。一句话 = 一个句号/问号/感叹号结尾的完整陈述。
2. 只能使用片段中**实际出现**的数字、金额、客户名、产品型号、日期，禁止推测和换算
3. 片段中没有的信息，直接回答「依据现有记录无法确认。」**绝对不要编造**
4. 用中文回答，简洁，不超过 3 句话

输出 JSON（不要有其他文字）：
{
  "answer": "带 [n] 引用标注的回答",
  "cited": [使用到的片段编号],
  "has_answer": true 或 false
}

【Few-shot 示例】
上下文：[1] (C04) Bright Home Co. 采购 LED Downlight DL-90，单价 USD 3.50/pc。[2] (C01) Global Import Ltd. 询盘 LED Panel Light PL-6060，报价 USD 12.80/pc。

问：哪些客户问过 LED Downlight？
答：{"answer": "依据现有记录，只有 Bright Home Co. 采购过 LED Downlight [1]。", "cited": [1], "has_answer": true}

问：Global Import Ltd. 的面板灯成交价？
答：{"answer": "依据现有记录，Global Import Ltd. 在 2025-11 首次询盘时报价 USD 12.80/pc，但未成交 [1]。", "cited": [1], "has_answer": true}

问：有没有客户投诉过质量问题？
答：{"answer": "依据现有记录，Bright Home Co. 曾反馈 LED Downlight 灯珠色温不一致的问题 [1]。", "cited": [1], "has_answer": true}
"""


def build_prompt(context_str, query):
    """构造用户 prompt（上下文 + 问题）。"""
    return f"""历史往来片段：
{context_str}

---
问题：{query}

请严格依据上述片段回答，并输出 JSON。"""


def _parse_json_content(content):
    """解析 LLM 返回的 JSON，容忍 markdown 代码块与前后废话。"""
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    # 兜底：截取第一个 { 到最后一个 }
    if "{" in text and "}" in text:
        text = text[text.index("{"): text.rindex("}") + 1]
    return json.loads(text)


def _template_fallback(query, context_str, id_map=None, purpose=""):
    """降级模板 —— LLM 不可用时的兜底产出。

    注意：模板本身无法做 LLM 生成约束，但会尽量打上 [n] 引用编号
    （基于 id_map），让下游忠实度/溯源评测仍能工作。
    对外不得声称这是「生成结果」，必须显式标记 mode=template_fallback。
    """
    id_map = id_map or {}
    body = context_str if context_str and context_str != EMPTY_CONTEXT else EMPTY_CONTEXT

    # 把 chunk 文本按 [n] 编号注入，方便评测侧溯源
    numbered_lines = []
    for num, cid in sorted(id_map.items(), key=lambda x: int(x[0])):
        if cid and cid in _CHUNK_CACHE:
            numbered_lines.append(f"  [ {num} ] ({cid}) {_CHUNK_CACHE[cid]}")

    if not numbered_lines:
        numbered_lines.append(f"  （无相关历史记录）")

    return {
        "answer": (
            f"【模板降级产出 · 未经 LLM 生成】\n"
            f"问题：{query}\n"
            f"相关历史片段：\n" + "\n".join(numbered_lines) + "\n"
            f"（请人工核对后回复）"
        ),
        "cited": [],
        "cited_ids": list(id_map.values()),
        "invalid_cites": [],
        "has_answer": bool(numbered_lines and numbered_lines[0] != "  （无相关历史记录）"),
        "mode": "template_fallback",
        "no_citation": False,  # 模板路径也有引用，只是非 LLM 生成
        "elapsed": 0.0,
        "est_cost": 0.0,
        "error": "",
        "raw": "",
    }


# ----------------------------------------------------------------------
# 生成器
# ----------------------------------------------------------------------
class AnswerGenerator:
    """带引用标注的 RAG 生成器。"""

    def __init__(self, api_key=None, timeout=DEFAULT_TIMEOUT, provider=None, verbose=True):
        self.verbose = verbose
        self.timeout = timeout
        self.call_count = 0
        self.fallback_count = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cost = 0.0

        if provider is not None:
            self.provider = provider
        elif DeepSeekProvider is None:
            self.provider = None
        else:
            key = api_key or self._load_deepseek_key()
            self.provider = DeepSeekProvider(key, timeout=timeout) if key else None

        if self.provider is None:
            print("[警告] 未拿到 DeepSeek API Key，生成器将全程走模板降级")

    @staticmethod
    def _load_deepseek_key():
        """复用项目既有配置读取逻辑，不重写一份。"""
        try:
            from llm_fallback import FallbackManager, CONFIG_FILE
            cfg = FallbackManager._load_config()
            return cfg.get("deepseek", {}).get("api_key") or os.environ.get("DEEPSEEK_API_KEY")
        except Exception:
            return os.environ.get("DEEPSEEK_API_KEY")

    # --------------------------------------------------------------
    def generate(self, query, context_str, id_map):
        """基于上下文生成带引用的答案。

        参数
            query       : 用户问题
            context_str : build_context() 产出的上下文（含 [n] 编号）
            id_map      : {"1": "C07", ...} 编号 → chunk id

        返回 dict（成功）：
            answer / cited / cited_ids / invalid_cites / has_answer
            mode="llm" / no_citation / elapsed / est_cost / raw
        """
        if not self.provider:
            self.fallback_count += 1
            return _template_fallback(query, context_str, id_map, "no_provider")

        prompt = build_prompt(context_str, query)
        t0 = time.time()
        try:
            raw = self.provider.raw_call(SYSTEM_PROMPT, prompt)
        except Exception as e:
            self.fallback_count += 1
            if self.verbose:
                print(f"[降级] LLM 调用失败（{type(e).__name__}: {e}）→ 回退模板")
            return _template_fallback(query, context_str, id_map, f"{type(e).__name__}: {e}")

        elapsed = time.time() - t0

        # —— 解析 ——
        try:
            data = _parse_json_content(raw)
            answer = str(data.get("answer", "")).strip()
            cited_field = data.get("cited") or []
            has_answer = bool(data.get("has_answer", True))
        except Exception as e:
            self.fallback_count += 1
            if self.verbose:
                print(f"[降级] JSON 解析失败（{e}）→ 回退模板")
            return _template_fallback(query, context_str, id_map, f"parse_failed: {e}")

        if not answer:
            self.fallback_count += 1
            return _template_fallback(query, context_str, id_map, "empty_answer")

        # —— 引用归一：答案内联 [n] 与 JSON cited 字段取并集 ——
        inline = extract_citations(answer)
        merged, seen = [], set()
        for n in inline + [str(c) for c in cited_field]:
            if n not in seen:
                seen.add(n)
                merged.append(n)
        cited_ids, invalid = map_citations(merged, id_map)

        # —— 成本估算（raw_call 不返回 usage，按字符粗估）——
        p_tok = est_tokens(SYSTEM_PROMPT + prompt)
        c_tok = est_tokens(answer)
        cost = p_tok * PRICE_IN_PER_TOKEN + c_tok * PRICE_OUT_PER_TOKEN
        self.call_count += 1
        self.total_prompt_tokens += p_tok
        self.total_completion_tokens += c_tok
        self.total_cost += cost

        result = {
            "answer": answer,
            "cited": merged,
            "cited_ids": cited_ids,
            "invalid_cites": invalid,
            "has_answer": has_answer,
            "mode": "llm",
            # 有上下文却一个引用都没有 = 严重风险，交给评测层计失败，不在此掩盖
            "no_citation": (not merged) and bool(context_str) and context_str != EMPTY_CONTEXT,
            "elapsed": round(elapsed, 3),
            "est_cost": round(cost, 6),
            "prompt_tokens": p_tok,
            "completion_tokens": c_tok,
            "error": None,
            "raw": raw,
        }
        self._log(query, context_str, result)
        return result

    def generate_from_bundle(self, bundle):
        """直接吃 context_builder 的返回，省一层解包。"""
        return self.generate(bundle["query"], bundle["context"], bundle["id_map"])

    # --------------------------------------------------------------
    def _log(self, query, context_str, result):
        """沿用项目已有的审计日志（output/llm_calls.jsonl），不另开文件。"""
        if not LOG_FILE:
            return
        try:
            os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
            rec = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "purpose": "rag_generate",
                "query": query,
                "context_chars": len(context_str or ""),
                "mode": result["mode"],
                "cited_ids": result["cited_ids"],
                "invalid_cites": result["invalid_cites"],
                "elapsed": result["elapsed"],
                "est_cost": result["est_cost"],
                "answer": result["answer"],
            }
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass  # 日志失败绝不能影响主流程

    def summary(self):
        return {
            "calls": self.call_count,
            "fallbacks": self.fallback_count,
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "est_cost_yuan": round(self.total_cost, 6),
        }


# ----------------------------------------------------------------------
# Demo
# ----------------------------------------------------------------------
DEMO_QUERIES = [
    "Global Import 的历史报价是多少？",
    "有没有客户投诉过质量问题？",
    "哪个客户对交期要求最严格？",
]


def main():
    ap = argparse.ArgumentParser(description="RAG 生成端：带引用标注的答案生成")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--bm25", action="store_true", help="检索只用 BM25（不调 embedding）")
    ap.add_argument("--query", type=str, default=None)
    args = ap.parse_args()

    from context_builder import _get_retriever, build_from_query  # 局部导入，避免循环依赖

    retr = _get_retriever(prefer_hybrid=not args.bm25)
    gen = AnswerGenerator(timeout=args.timeout)
    queries = [args.query] if args.query else DEMO_QUERIES

    for q in queries:
        bundle = build_from_query(q, retr, top_k=args.top_k)
        res = gen.generate_from_bundle(bundle)

        print(f"\n{'=' * 70}")
        print(f"Query: {q}")
        print("-" * 70)
        print("【上下文】")
        print(bundle["context"])
        print("-" * 70)
        print(f"【答案】mode={res['mode']}")
        print(res["answer"])
        print("-" * 70)
        print(f"引用编号 {res['cited']} → chunk {res['cited_ids']}")
        if res["invalid_cites"]:
            print(f"⚠ 无效引用（模型编造）: {res['invalid_cites']}")
        if res["no_citation"]:
            print("⚠ 有上下文却零引用 —— 评测层会记为失败项")
        print(f"耗时 {res['elapsed']}s | 估算成本 ¥{res['est_cost']:.6f} | has_answer={res['has_answer']}")

    print(f"\n{'=' * 70}")
    print("汇总:", json.dumps(gen.summary(), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
