# -*- coding: utf-8 -*-
"""
邮件流水线重编排为状态图（JD #4 落地）
=====================================

用自研 `state_graph.py`（langgraph 等价抽象）把 Day01-06 的流水线重画成图。
重点不是"换个写法"，而是**图能表达、链表达不了的三件事**：

1. **条件边跳过检索**：通知类邮件（付款/发货/清关）直接进 compose，不付检索的钱。
   —— 复用第 8 节多智能体的结论，但**省掉主管那一跳**。
2. **循环重试（cycle）**：generate 后若没引用到任何依据 → 回到 retrieve 换更宽的 query 再跑一遍，
   最多 1 次。if-else 流水线写不出"回头重试"，这是图相对链的本质优势。
3. **HITL 节点**：分类置信度过低 / 两次检索仍无依据 → 路由到 human_review 节点停下等人工。

检查点：每步状态落盘 `output/graph_checkpoint.json`，支持断点续跑 + 审计。

运行：python src/graph_pipeline.py
作者：麦当 · 2026-09-04（Day06 续 · JD #4）
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from state_graph import StateGraph, Checkpointer  # noqa: E402

try:
    from classifier import score_email
except Exception:
    score_email = None
try:
    from extractor import extract_fields as _extract_fields_rule
except Exception:
    _extract_fields_rule = None
try:
    from vector_retriever import build_hybrid
except Exception:
    build_hybrid = None
try:
    from context_builder import build_context
except Exception:
    build_context = None
try:
    from generator import AnswerGenerator
except Exception:
    AnswerGenerator = None
try:
    from reranker import LLMReranker
except Exception:
    LLMReranker = None
try:
    from llm_fallback import DeepSeekProvider, FallbackManager
except Exception:
    DeepSeekProvider, FallbackManager = None, None

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 计价与项目其余部分一致
PRICE_IN = 1.0 / 1_000_000
PRICE_OUT = 8.0 / 1_000_000

# 注意：本项目的 DeepSeekProvider.chat() 在 tools=None 时会强制下发
# response_format=json_object（见 llm_fallback），所以 compose 必须**要 JSON 回来**，
# 再解析出正文——与 eval_agent.py 的 H3 compose_reply 同一套模式，不要改成纯文本。
COMPOSE_SYSTEM = (
    "你是外贸业务员。基于下面由检索系统给出的【事实片段】，写一封给客户的英文回信。"
    "要求：专业、简洁；只使用片段中出现的事实，片段没有的信息写"
    "'we will verify internally and revert to you shortly'，严禁编造价格/交期。"
    "结尾署名 Export Department。\n"
    "只返回 JSON，格式：{\"reply\": \"英文回信正文\"}"
)
ACK_SYSTEM = (
    "你是外贸业务员。这是一条通知类邮件（付款/发货/清关），无需引用历史记录。"
    "写一封简短的英文确认回信：确认收到、说明下一步动作。不要编造任何数字。\n"
    "只返回 JSON，格式：{\"reply\": \"英文回信正文\"}"
)


def _parse_reply(content):
    """从 LLM 返回的 JSON 里取出 reply 字段；解析失败就退回原文。"""
    text = (content or "").strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 2:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
    if "{" in text and "}" in text:
        text = text[text.index("{"): text.rindex("}") + 1]
    try:
        d = json.loads(text)
        return str(d.get("reply", "")).strip() or text
    except Exception:
        return text


# ----------------------------------------------------------------------
# 节点实现：fn(state) -> state（就地修改并返回）
# ----------------------------------------------------------------------
def _default_query(state):
    f = state.get("fields") or {}
    parts = [f.get("customer"), f.get("product")]
    parts = [p for p in parts if p]
    if parts:
        return " ".join(parts)
    m = state.get("mail", {})
    return f"{m.get('subject', '')} {m.get('body', '')[:80]}"


def _broaden(state):
    """重试时换更宽的 query：直接用主题 + 正文开头（语义更泛）。"""
    m = state.get("mail", {})
    return f"{m.get('subject', '')} {m.get('body', '')[:160]}"


def _get_retriever(state):
    if state.get("_retriever") is None:
        if build_hybrid is None:
            raise RuntimeError("vector_retriever 不可用")
        state["_retriever"], _ = build_hybrid()
    return state["_retriever"]


def _get_provider(state):
    if state.get("_provider") is not None:
        return state["_provider"]
    if FallbackManager is None or DeepSeekProvider is None:
        return None
    cfg = FallbackManager._load_config()
    key = cfg.get("deepseek", {}).get("api_key") or os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    state["_provider"] = DeepSeekProvider(key)
    return state["_provider"]


def _n_classify(state):
    m = state["mail"]
    cat, conf, scores, hits = score_email(m.get("subject", ""), m.get("body", ""))
    state["category"] = cat
    state["confidence"] = round(float(conf), 3)
    state["cls_scores"] = {k: round(float(v), 2) for k, v in scores.items()}
    return state


def _n_extract(state):
    raw = _extract_fields_rule(state["mail"]) or {}
    state["fields"] = {k: (v or {}).get("value") for k, v in raw.items()}
    return state


def _n_retrieve(state):
    r = _get_retriever(state)
    if state.get("retry_n"):
        q = _broaden(state)
    else:
        q = state.get("query") or _default_query(state)
    hits = r.search(q, top_k=state.get("top_k", 5))
    state["query"] = q
    state["hits"] = hits
    state.setdefault("queries_tried", []).append(q[:60])
    return state


def _n_rerank(state):
    if LLMReranker is None or not state.get("hits"):
        return state
    rr = LLMReranker(verbose=False)
    state["hits"] = rr.rerank(state.get("query", ""), state["hits"], top_k=3)
    state["reranked"] = True
    return state


def _n_build_context(state):
    bundle = build_context(state.get("query", ""), state["hits"], max_chars=2000)
    state["bundle"] = bundle
    return state


def _n_generate(state):
    gen = AnswerGenerator()
    b = state["bundle"]
    res = gen.generate(state.get("query", ""), b["context"], b["id_map"])
    state["rag_answer"] = res.get("answer")
    state["cited_ids"] = res.get("cited_ids", [])
    state["cost"] = state.get("cost", 0.0) + float(res.get("est_cost") or 0)
    return state


def _n_compose(state):
    """把事实片段合成英文回信；通知类（无 RAG）走简短确认分支。"""
    provider = _get_provider(state)
    if provider is None:
        state["draft"] = state.get("rag_answer") or "[无 provider，未合成回信]"
        return state
    if state.get("category") == "notification" and not state.get("rag_answer"):
        messages = [
            {"role": "system", "content": ACK_SYSTEM},
            {"role": "user", "content": f"客户邮件：\n主题：{state['mail'].get('subject')}\n"
                                        f"正文：{state['mail'].get('body')}"},
        ]
    else:
        messages = [
            {"role": "system", "content": COMPOSE_SYSTEM},
            {"role": "user", "content":
                f"客户邮件：\n主题：{state['mail'].get('subject')}\n正文：{state['mail'].get('body')}\n\n"
                f"【事实片段】\n{state.get('rag_answer') or '（无）'}\n"
                f"引用编号：{state.get('cited_ids') or '无'}"},
        ]
    msg, usage = provider.chat(messages=messages, tools=None)
    state["draft"] = _parse_reply(msg.get("content"))
    pin = (usage or {}).get("prompt_tokens", 0) or 0
    pout = (usage or {}).get("completion_tokens", 0) or 0
    state["cost"] = state.get("cost", 0.0) + pin * PRICE_IN + pout * PRICE_OUT
    return state


def _n_human_review(state):
    state["needs_review"] = True
    state.setdefault("review_reason", "触发人工审核节点")
    return state


# ----------------------------------------------------------------------
# 路由（条件边）
# ----------------------------------------------------------------------
def _route_after_classify(state):
    if state.get("category") == "notification":
        return "skip_retrieve"
    if (state.get("confidence") or 1.0) < 0.45:
        state["review_reason"] = f"分类置信度过低（{state.get('confidence')}）"
        return "review"
    return "normal"


def _route_after_generate(state):
    if not state.get("cited_ids"):
        if state.get("retry_n", 0) < 1:
            state["retry_n"] = state.get("retry_n", 0) + 1
            return "retry"
        state["review_reason"] = "换宽 query 重试后仍无引用依据，需人工确认"
        return "review"
    return "compose"


def build_email_graph(checkpoint_path=None, verbose=True):
    """构建邮件处理状态图。"""
    g = StateGraph()
    g.add_node("classify", _n_classify)
    g.add_node("extract", _n_extract)
    g.add_node("retrieve", _n_retrieve)
    g.add_node("rerank", _n_rerank)
    g.add_node("build_context", _n_build_context)
    g.add_node("generate", _n_generate)
    g.add_node("compose", _n_compose)
    g.add_node("human_review", _n_human_review)

    # 条件边 1：通知类跳过检索；低置信度转人工
    g.add_conditional_edges("classify", _route_after_classify,
                            {"normal": "extract", "skip_retrieve": "compose",
                             "review": "human_review"})
    # 主干链
    g.add_edge("extract", "retrieve")
    g.add_edge("retrieve", "rerank")
    g.add_edge("rerank", "build_context")
    g.add_edge("build_context", "generate")
    # 条件边 2：无依据 → 回到 retrieve 重试（形成 cycle）；重试仍失败 → 人工
    g.add_conditional_edges("generate", _route_after_generate,
                            {"compose": "compose", "retry": "retrieve",
                             "review": "human_review"})

    g.set_entry_point("classify")
    g.set_finish("compose", "human_review")
    cp = Checkpointer(checkpoint_path) if checkpoint_path else None
    return g.compile(checkpointer=cp, max_steps=25, verbose=verbose)


def main():
    ap = argparse.ArgumentParser(description="状态图编排的邮件流水线（JD #4）")
    ap.add_argument("--ids", default="E01,E10,E09", help="要跑的邮件 id，逗号分隔")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    with open(os.path.join(BASE, "data", "e2e_emails.json"), encoding="utf-8") as f:
        emails = json.load(f)
    wanted = [s.strip() for s in args.ids.split(",") if s.strip()]
    picked = [e for e in emails if e.get("id") in wanted]

    results = []
    for mail in picked:
        print("\n" + "=" * 64)
        print(f"邮件 {mail.get('id')} | {mail.get('subject')}")
        print("=" * 64)
        cp_path = os.path.join(BASE, "output", f"graph_checkpoint_{mail.get('id')}.json")
        graph = build_email_graph(cp_path, verbose=not args.quiet)
        state = {"mail": mail, "cost": 0.0, "retry_n": 0, "retriever": None}
        t0 = time.time()
        st, trace = graph.run(state)
        elapsed = round(time.time() - t0, 2)

        path = " → ".join(t["node"] for t in trace)
        print(f"  路径: {path}")
        print(f"  分类: {st.get('category')} (置信度 {st.get('confidence')}) | "
              f"重试 {st.get('retry_n', 0)} 次 | 引用 {st.get('cited_ids') or '无'}")
        print(f"  人工审核: {'是（' + str(st.get('review_reason')) + '）' if st.get('needs_review') else '否'}")
        print(f"  成本 ¥{st.get('cost', 0):.5f} | 耗时 {elapsed}s | 步骤 {len(trace)}")
        draft = (st.get("draft") or "").replace("\n", " ")
        print(f"  回信: {draft[:260]}{'...' if len(draft) > 260 else ''}")

        results.append({
            "id": mail.get("id"), "category": st.get("category"),
            "confidence": st.get("confidence"), "path": path,
            "retry_n": st.get("retry_n", 0), "cited_ids": st.get("cited_ids"),
            "needs_review": bool(st.get("needs_review")),
            "review_reason": st.get("review_reason"),
            "cost_yuan": round(st.get("cost", 0.0), 6), "elapsed_sec": elapsed,
            "steps": len(trace), "queries_tried": st.get("queries_tried"),
            "draft": st.get("draft"),
        })

    with open(os.path.join(BASE, "output", "graph_pipeline_demo.json"), "w",
              encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n✔ 落盘 output/graph_pipeline_demo.json")


if __name__ == "__main__":
    main()
