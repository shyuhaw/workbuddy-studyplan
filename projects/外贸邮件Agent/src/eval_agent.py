# -*- coding: utf-8 -*-
"""
A/B 评测：固定流水线 vs Agent 自主规划
======================================
Day06 的硬指标。目的不是"证明 Agent 更好"，而是**测出它到底好不好、好在哪、差在哪**。

为什么这个评测比代码本身重要
----------------------------
JD 高频句：「理解大模型**能力边界与工程取舍**」。
「我用了 Function Calling」是一句陈述；
「我实测了 20 条，Agent 自主规划准确率 A、成本 B，固定流水线准确率 C、成本 D，
 所以步骤确定的场景我选流水线」—— 这才叫工程取舍，而且是能讲 3 分钟的故事。

成本口径（关键，否则对比无效）
------------------------------
两边调用的模块完全不同（分类/提取/生成 vs 多轮 Function Calling），
各自统计必然口径不一致。所以统一走 llm_fallback.TOKEN_TOTAL 全局计数器 ——
所有 LLM 调用最终都经过 DeepSeekProvider.chat()，两边可比、都是真实开销。

归因五类（只统计，不美化）
--------------------------
    skipped_retrieve  没检索就产出答案（可能是合理跳过，也可能是漏）
    extra_call        同一工具调用 >1 次（纯浪费）
    order_error       generate 前没 build_context / build_context 前没 retrieve（硬错误）
    param_error       工具执行失败（ok=False）
    fallback          触发降级，没走完 Agent 路径

作者：麦当
日期：2026-09-04（Day06）
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from llm_fallback import (  # noqa: E402
    TOKEN_TOTAL, reset_token_total, calc_cost, FallbackManager, DeepSeekProvider,
)
from agent_loop import AgentLoop  # noqa: E402
from workflow import WorkflowCase  # noqa: E402
from agent import MailAgent  # noqa: E402
from tools import ToolSession, make_tools, execute_tool  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(BASE_DIR, "data", "e2e_emails.json")
OUT_PATH = os.path.join(BASE_DIR, "output", "eval_agent.json")

NO_ANSWER_MARK = "依据现有记录无法确认"
MIN_USEFUL_LEN = 50

# 标准路径：这 5 步是「要产出带依据的回复」的必需项；rerank_history 是可选优化项
REQUIRED_STEPS = ["classify_email", "extract_fields", "retrieve_history",
                  "build_context", "generate_answer"]


# 账单类故障标记：遇到这些说明不是模型能力问题，是账户没钱了 → 样本无效，必须剔除
BILLING_MARKERS = ("402", "Payment Required", "insufficient balance", "Insufficient Balance")


def is_billing_error(text):
    return any(m in (text or "") for m in BILLING_MARKERS)


def is_invalid_sample(r):
    """判定样本是否有效。

    两条剔除规则（都不是"删失败样本"，是剔除**非能力因素**导致的无效数据）：
    1. 任一侧出现账单类故障（402 余额不足）→ 模型根本没机会跑
    2. A 组 usage.calls == 0 → 说明没调成 LLM，走的是规则层降级产物，
       拿它和真跑了 LLM 的样本比成本是**不公平的对比**
    """
    a, b = r["A_pipeline"], r["B_agent"]
    for side in (a, b):
        for field in ("error", "stop_reason", "fallback_reason"):
            if is_billing_error(side.get(field)):
                return True
    if (a.get("usage") or {}).get("calls", 0) == 0:
        return True
    return False


def check_api_alive():
    """每条开跑前做一次健康检查 —— 余额耗尽时立刻停，别烧一堆无效调用。

    这是 Day06 踩出来的坑：20 条评测跑到第 12 条时账户余额耗尽，
    后面 9 条全部 402，却照样跑完、照样落盘，产出一批看起来正常、实则无效的数据。
    """
    try:
        cfg = FallbackManager._load_config()
        key = cfg.get("deepseek", {}).get("api_key") or os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            return False, "无 API Key"
        p = DeepSeekProvider(key, timeout=15)
        # 注意：DeepSeek 的 json_object 模式要求 prompt 里出现 "json" 字样
        p.chat(messages=[{"role": "user", "content": 'Reply with JSON: {"ok":1}'}])
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def is_useful(answer):
    """答案是否「可用」：非空、够长、且不是一句"无法确认"就交差。"""
    if not answer:
        return False
    if NO_ANSWER_MARK in answer and len(answer) < 120:
        return False
    return len(answer) >= MIN_USEFUL_LEN


def run_pipeline(mail, query_mode="simple"):
    """A 组：Day01-05 的固定流水线。"""
    reset_token_total()
    t0 = time.time()
    try:
        res = MailAgent().process_one(mail)
        case = WorkflowCase(res, query_mode=query_mode).run_pipeline()
        answer = case.draft or ""
        cited = case.cited_ids or []
        gen_mode = case.gen_mode
        err = None
    except Exception as e:
        answer, cited, gen_mode = "", [], None
        err = f"{type(e).__name__}: {e}"
    elapsed = round(time.time() - t0, 3)
    return {
        "answer": answer,
        "cited_ids": cited,
        "gen_mode": gen_mode,
        "useful": is_useful(answer),
        "has_citation": bool(cited),
        "answer_len": len(answer),
        "elapsed_sec": elapsed,
        "cost_yuan": round(calc_cost(), 6),
        "usage": dict(TOKEN_TOTAL),
        "error": err,
    }


def attribute(trace, fallback_reason):
    """把 Agent 的行为归因到五类（只统计，不美化）。"""
    seq = [t["tool"] for t in trace if t["tool"] != "finish"]
    tags = []

    if "retrieve_history" not in seq:
        tags.append("skipped_retrieve")

    # 同工具重复调用（finish 不算）
    seen = {}
    for s in seq:
        seen[s] = seen.get(s, 0) + 1
    if any(v > 1 for v in seen.values()):
        tags.append("extra_call")

    # 顺序硬错误
    def idx(name):
        return seq.index(name) if name in seq else -1

    i_ret, i_ctx, i_gen = idx("retrieve_history"), idx("build_context"), idx("generate_answer")
    if i_gen >= 0 and i_ctx < 0:
        tags.append("order_error")
    elif i_ctx >= 0 and i_ret < 0:
        tags.append("order_error")
    elif i_gen >= 0 and i_ctx > i_gen:
        tags.append("order_error")

    if any(not t.get("ok", True) for t in trace):
        tags.append("param_error")

    if fallback_reason:
        tags.append("fallback")

    return tags, seq


def run_agent(mail, max_rounds=6):
    """B 组：Agent 自主规划。"""
    reset_token_total()
    t0 = time.time()
    try:
        res = AgentLoop(max_rounds=max_rounds, verbose=False).run(mail)
    except Exception as e:
        res = {
            "mode": "pipeline_fallback", "answer": "", "cited_ids": [],
            "finished": False, "rounds": 0, "tool_calls": 0, "trace": [],
            "fallback_reason": f"外层异常 {type(e).__name__}: {e}",
            "elapsed_sec": 0, "cost_yuan": 0, "usage": {},
        }
    elapsed = round(time.time() - t0, 3)
    tags, seq = attribute(res.get("trace", []), res.get("fallback_reason"))
    answer = res.get("answer", "")
    return {
        "mode": res.get("mode"),
        "finished": res.get("finished", False),
        "stop_reason": res.get("stop_reason"),
        "fallback_reason": res.get("fallback_reason"),
        "answer": answer,
        "cited_ids": res.get("cited_ids", []),
        "useful": is_useful(answer),
        "has_citation": bool(res.get("cited_ids")),
        "answer_len": len(answer),
        "rounds": res.get("rounds", 0),
        "tool_calls": res.get("tool_calls", 0),
        "tool_seq": seq,
        "tags": tags,
        "elapsed_sec": elapsed,
        "cost_yuan": round(calc_cost(), 6),
        "usage": dict(TOKEN_TOTAL),
    }


# ---------------------------------------------------------------------------
# H3：统一任务定义为「写客户回信」的干净对比（固定编排 vs 自主编排，同工具同生成器）
# ---------------------------------------------------------------------------
# 目的：把 A 也改成「写客户回信」+ 复用同一套 tools + 同一 AnswerGenerator，
# 只把工具顺序写死（不动模型决策）。这样 A 与 B 唯一的变量就是「编排方式」，
# 从而公平回答"该不该上 Agent"——而不是被"任务定义不同"这个混淆变量带偏。
COMPOSE_SYSTEM_PROMPT = """你是一个外贸业务助理。下面给你一封客户邮件，以及从客户历史档案中检索到的「事实片段（带 [n] 编号）」。

请据此起草一封给该客户的英文回复邮件。规则：
1. 所有价格、规格、交期、历史往来等事实性陈述，必须基于给定片段，并标注 [n] 引用。
2. 片段里没有的信息（例如客户首次询盘、无历史成交价），不要编造；
   用 "We will check and revert shortly." 之类的话术妥善回应并承诺跟进。
3. 用英语、自然、专业；以 "Dear {customer}," 开头，以 "Best regards," 结尾。
4. 不要写内部备注、不要写中文、不要出现「依据现有记录」这类内部措辞。

只输出 JSON：{"reply": "完整英文邮件正文", "cited": [用到的片段编号]}
"""


def _h3_provider():
    try:
        cfg = FallbackManager._load_config()
        key = cfg.get("deepseek", {}).get("api_key") or os.environ.get("DEEPSEEK_API_KEY")
        if key:
            return DeepSeekProvider(key, timeout=30)
    except Exception:
        pass
    return None


def _parse_compose(content):
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    if "{" in text and "}" in text:
        text = text[text.index("{"): text.rindex("}") + 1]
    try:
        d = json.loads(text)
        return {"reply": str(d.get("reply", "")).strip(),
                "cited": [str(c) for c in (d.get("cited") or [])]}
    except Exception:
        return {"reply": text, "cited": []}


def compose_reply(provider, mail, rag_answer, cited_ids):
    """把 RAG 事实片段 + 邮件，合成一封给客户的英文回信（A3 与 B 共享同一事实来源）。"""
    cust = (mail.get("from") or mail.get("customer") or "Customer")
    user = (
        f"客户邮件：\n主题：{mail.get('subject', '')}\n正文：{mail.get('body', '')}\n\n"
        f"检索到的历史事实片段：\n{rag_answer}\n\n"
        f"请起草给 {cust} 的英文回复。"
    )
    try:
        message, _ = provider.chat(
            messages=[
                {"role": "system", "content": COMPOSE_SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ]
        )
        data = _parse_compose(message.get("content") or "")
        return data.get("reply") or "", data.get("cited") or []
    except Exception:
        return "", []


def run_pipeline_reply(mail):
    """A3：固定编排版「写客户回信」—— 与 B 共用同一套工具与生成器，只把顺序写死。

    路径：extract → retrieve(top5) → rerank(top3) → build_context → generate → compose
    """
    reset_token_total()
    t0 = time.time()
    try:
        session = ToolSession(mail)
        _, impl = make_tools(session)
        ex = execute_tool("extract_fields", {}, impl, session)
        cust = prod = ""
        try:
            exd = json.loads(ex)
            cust = (exd.get("customer", {}) or {}).get("value") or ""
            prod = (exd.get("product", {}) or {}).get("value") or ""
        except Exception:
            pass
        query = f"{cust} {prod}".strip() or mail.get("subject", "")
        execute_tool("retrieve_history", {"query": query, "top_k": 5}, impl, session)
        execute_tool("rerank_history", {"query": query, "top_k": 3}, impl, session)
        execute_tool("build_context", {"query": query, "max_chars": 2000}, impl, session)
        g = execute_tool("generate_answer", {"query": query}, impl, session)
        rag_answer, cited = "", []
        try:
            gd = json.loads(g)
            rag_answer = gd.get("answer") or ""
            cited = gd.get("cited_ids") or []
        except Exception:
            pass
        provider = _h3_provider()
        answer, _ = compose_reply(provider, mail, rag_answer, cited) if provider else ("", [])
        if not answer:
            answer = rag_answer  # 极端降级：至少返回事实片段
        err = None
    except Exception as e:
        answer, cited, err = "", [], f"{type(e).__name__}: {e}"
    elapsed = round(time.time() - t0, 3)
    return {
        "answer": answer,
        "cited_ids": cited,
        "useful": bool(answer) and len(answer) >= MIN_USEFUL_LEN,
        "has_citation": bool(cited),
        "answer_len": len(answer),
        "mode": "pipeline_reply",
        "elapsed_sec": elapsed,
        "cost_yuan": round(calc_cost(), 6),
        "usage": dict(TOKEN_TOTAL),
        "error": err,
    }


def is_invalid_sample_h3(r):
    """H3 无效样本判定（同 is_invalid_sample 口径）：账单故障 或 A3 未真调 LLM。"""
    a, b = r["A3_pipeline_reply"], r["B_agent"]
    for side in (a, b):
        for field in ("error", "stop_reason", "fallback_reason"):
            if is_billing_error(side.get(field)):
                return True
    if (a.get("usage") or {}).get("calls", 0) == 0:
        return True
    return False


def _run_h3(args):
    """H3：固定编排写回信(A3) vs Agent 自主(B)，共用工具与生成器，隔离「编排方式」单变量。"""
    with open(DATA_PATH, encoding="utf-8") as f:
        emails = json.load(f)
    if args.limit:
        emails = emails[:args.limit]

    print(f"H3 干净对比：固定编排写回信(A3) vs Agent 自主(B) · {len(emails)} 封\n")
    results = []
    for i, mail in enumerate(emails, 1):
        alive, reason = check_api_alive()
        if not alive:
            print(f"\n⚠️ API 不可用，H3 在第 {i} 条中止：{reason}")
            break
        a3 = run_pipeline_reply(mail)
        b = run_agent(mail, max_rounds=args.rounds)
        results.append({"mail_id": mail.get("id"), "subject": mail.get("subject", ""),
                        "A3_pipeline_reply": a3, "B_agent": b})
        print(f"[{i}/{len(emails)}] {mail.get('id')} "
              f"A3:{'✅' if a3['useful'] else '❌'} ¥{a3['cost_yuan']:.5f} | "
              f"B:{'✅' if b['useful'] else '❌'} ¥{b['cost_yuan']:.5f} {b['rounds']}轮")

    invalid = [r for r in results if is_invalid_sample_h3(r)]
    if invalid:
        print(f"\n剔除无效样本 {len(invalid)} 条（非模型能力因素：账单故障或未调成 LLM）")
        results = [r for r in results if not is_invalid_sample_h3(r)]
    if not results:
        print("无有效样本"); return

    n = len(results)

    def rate(key, grp):
        return round(sum(1 for r in results if r[grp][key]) / n, 3)

    def avg(key, grp):
        vals = [r[grp][key] for r in results if isinstance(r[grp].get(key), (int, float))]
        return round(sum(vals) / len(vals), 4) if vals else 0

    def total(key, grp):
        return round(sum(r[grp].get(key, 0) or 0 for r in results), 6)

    def total_calls(grp):
        return sum(r[grp].get("usage", {}).get("calls", 0) or 0 for r in results)

    summary = {
        "n_samples": n, "max_rounds": args.rounds, "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "A3_pipeline_reply": {
            "useful_rate": rate("useful", "A3_pipeline_reply"),
            "citation_rate": rate("has_citation", "A3_pipeline_reply"),
            "avg_answer_len": avg("answer_len", "A3_pipeline_reply"),
            "avg_cost_yuan": avg("cost_yuan", "A3_pipeline_reply"),
            "total_cost_yuan": total("cost_yuan", "A3_pipeline_reply"),
            "total_llm_calls": total_calls("A3_pipeline_reply"),
        },
        "B_agent": {
            "useful_rate": rate("useful", "B_agent"),
            "citation_rate": rate("has_citation", "B_agent"),
            "avg_answer_len": avg("answer_len", "B_agent"),
            "avg_cost_yuan": avg("cost_yuan", "B_agent"),
            "total_cost_yuan": total("cost_yuan", "B_agent"),
            "total_llm_calls": total_calls("B_agent"),
            "avg_rounds": avg("rounds", "B_agent"),
            "finished_rate": rate("finished", "B_agent"),
            "fallback_count": sum(1 for r in results if r["B_agent"].get("fallback_reason")),
        },
    }
    out_path = OUT_PATH.replace("eval_agent.json", "eval_agent_h3.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "results": results}, f, ensure_ascii=False, indent=2)

    A3, B = summary["A3_pipeline_reply"], summary["B_agent"]
    print("\n" + "=" * 66)
    print(f"H3 干净对比（{n} 条 · 同工具同生成器，只编排方式不同）")
    print("=" * 66)
    print(f"{'指标':<20}{'A3 固定编排':>16}{'B Agent 自主':>16}{'差值':>14}")
    print("-" * 66)
    print(f"{'可用率':<20}{A3['useful_rate']:>15}{B['useful_rate']:>16}"
          f"{B['useful_rate']-A3['useful_rate']:>+13.3f}")
    print(f"{'有引用比例':<20}{A3['citation_rate']:>15}{B['citation_rate']:>16}")
    print(f"{'平均答案长度':<20}{A3['avg_answer_len']:>13}字{B['avg_answer_len']:>14}字")
    print(f"{'单次成本':<20}{A3['avg_cost_yuan']:>14}¥{B['avg_cost_yuan']:>14}¥"
          f"{B['avg_cost_yuan']-A3['avg_cost_yuan']:>+12.5f}¥")
    print(f"{'总成本':<20}{A3['total_cost_yuan']:>13}¥{B['total_cost_yuan']:>14}¥")
    print(f"{'LLM调用总次数':<20}{A3['total_llm_calls']:>15}{B['total_llm_calls']:>16}")
    print(f"{'平均轮数':<20}{'—':>16}{B['avg_rounds']:>16}")
    print(f"{'finish完成率':<20}{'—':>16}{B['finished_rate']:>16}")
    print(f"{'降级次数':<20}{'0':>16}{B['fallback_count']:>16}")
    print(f"\n落盘：{out_path}")


def main():
    ap = argparse.ArgumentParser(description="A/B 评测：固定流水线 vs Agent 自主规划")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（默认全跑 20 条）")
    ap.add_argument("--rounds", type=int, default=6, help="Agent 最大轮数")
    ap.add_argument("--query-mode", choices=["simple", "optimized"], default="simple",
                    help="Pipeline query 构造模式：simple=原始，optimized=带意图词")
    ap.add_argument("--reuse", help="从已有 eval json 重新汇总（不重跑，用于余额中断后补救）")
    ap.add_argument("--h3", action="store_true",
                    help="H3 干净对比：固定编排写回信(A3) vs Agent 自主(B)，共用工具与生成器")
    args = ap.parse_args()

    results, abort_reason = [], None

    if args.h3:
        return _run_h3(args)

    if args.reuse:
        # —— 复用模式：不重跑，从已有结果里剔除无效样本后重新出结论 ——
        with open(args.reuse, encoding="utf-8") as f:
            data = json.load(f)
        results = data["results"]
        print(f"复用已有结果：{args.reuse}（共 {len(results)} 条原始记录）\n")
    else:
        with open(DATA_PATH, encoding="utf-8") as f:
            emails = json.load(f)
        if args.limit:
            emails = emails[:args.limit]

        print(f"评测样本：{len(emails)} 封真实邮件\n")
        for i, mail in enumerate(emails, 1):
            # 每条开跑前体检：账户没钱就立刻停，别烧一堆 402
            alive, reason = check_api_alive()
            if not alive:
                abort_reason = reason
                print(f"\n⚠️ API 不可用，评测在第 {i} 条中止：{reason}")
                print(f"   已产出 {len(results)} 条有效样本（后续未跑）")
                break

            print(f"[{i}/{len(emails)}] {mail.get('id')} {mail.get('subject', '')[:40]} ... ",
                  end="", flush=True)
            a = run_pipeline(mail, query_mode=args.query_mode)
            b = run_agent(mail, max_rounds=args.rounds)
            results.append({"mail_id": mail.get("id"), "subject": mail.get("subject", ""),
                            "category": mail.get("category", ""),
                            "A_pipeline": a, "B_agent": b})
            print(f"A: {'✅' if a['useful'] else '❌'} ¥{a['cost_yuan']:.5f} | "
                  f"B: {'✅' if b['useful'] else '❌'} ¥{b['cost_yuan']:.5f} "
                  f"{b['rounds']}轮/{b['tool_calls']}调 "
                  f"{('⚠' + ','.join(b['tags'])) if b['tags'] else ''}")

    # —— 剔除无效样本（账单故障 / 没真调 LLM 的降级产物）——
    invalid = [r for r in results if is_invalid_sample(r)]
    if invalid:
        print(f"\n剔除无效样本 {len(invalid)} 条（非模型能力因素：账单故障或未调成 LLM）")
        for r in invalid:
            print(f"  · {r['mail_id']}  {r['B_agent'].get('stop_reason') or 'A 组未调 LLM'}")
        results = [r for r in results if not is_invalid_sample(r)]

    if not results:
        print("\n无有效样本，评测终止。")
        return

    n = len(results)

    def rate(key, grp):
        return round(sum(1 for r in results if r[grp][key]) / n, 3)

    def avg(key, grp):
        vals = [r[grp][key] for r in results if isinstance(r[grp].get(key), (int, float))]
        return round(sum(vals) / len(vals), 4) if vals else 0

    def total(key, grp):
        return round(sum(r[grp].get(key, 0) or 0 for r in results), 6)

    def total_calls(grp):
        """LLM 调用次数在 usage 子字典里，单独取"""
        return sum(r[grp].get("usage", {}).get("calls", 0) or 0 for r in results)

    summary = {
        "n_samples": n,
        "n_invalid_excluded": len(invalid),
        "abort_reason": abort_reason,
        "max_rounds": args.rounds,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "A_pipeline": {
            "useful_rate": rate("useful", "A_pipeline"),
            "citation_rate": rate("has_citation", "A_pipeline"),
            "avg_answer_len": avg("answer_len", "A_pipeline"),
            "avg_elapsed_sec": avg("elapsed_sec", "A_pipeline"),
            "total_cost_yuan": total("cost_yuan", "A_pipeline"),
            "avg_cost_yuan": avg("cost_yuan", "A_pipeline"),
            "total_llm_calls": total_calls("A_pipeline"),
            "errors": sum(1 for r in results if r["A_pipeline"].get("error")),
        },
        "B_agent": {
            "useful_rate": rate("useful", "B_agent"),
            "citation_rate": rate("has_citation", "B_agent"),
            "avg_answer_len": avg("answer_len", "B_agent"),
            "avg_elapsed_sec": avg("elapsed_sec", "B_agent"),
            "total_cost_yuan": total("cost_yuan", "B_agent"),
            "avg_cost_yuan": avg("cost_yuan", "B_agent"),
            "total_llm_calls": total_calls("B_agent"),
            "avg_rounds": avg("rounds", "B_agent"),
            "avg_tool_calls": avg("tool_calls", "B_agent"),
            "finished_rate": rate("finished", "B_agent"),
            "fallback_count": sum(1 for r in results if r["B_agent"].get("fallback_reason")),
        },
    }

    # 归因分布
    tag_count = {}
    for r in results:
        for t in r["B_agent"]["tags"]:
            tag_count[t] = tag_count.get(t, 0) + 1
    summary["B_agent"]["tag_distribution"] = tag_count

    # 逐条差异（B 更差的样本）
    worse = []
    for r in results:
        a, b = r["A_pipeline"], r["B_agent"]
        if (a["useful"] and not b["useful"]) or b["cost_yuan"] > a["cost_yuan"] * 2:
            worse.append({
                "mail_id": r["mail_id"],
                "subject": r["subject"],
                "reason": ("答案不可用" if (a["useful"] and not b["useful"])
                           else "成本超 2 倍"),
                "A_cost": a["cost_yuan"], "B_cost": b["cost_yuan"],
                "tags": b["tags"],
                "tool_seq": b["tool_seq"],
                "fallback_reason": b["fallback_reason"],
            })
    summary["worse_samples"] = worse

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "results": results}, f, ensure_ascii=False, indent=2)

    # ---------------- 打印 ----------------
    A, B = summary["A_pipeline"], summary["B_agent"]
    print("\n" + "=" * 68)
    print(f"A/B 评测结果（{n} 条有效样本 · 成本口径统一为全链路 token）")
    print("=" * 68)
    print(f"{'指标':<22}{'A 固定流水线':>16}{'B Agent 自主':>16}{'差值':>14}")
    print("-" * 68)

    def row(label, ka, kb, unit="", invert=False):
        va, vb = A[ka], B[kb]
        diff = vb - va
        mark = ""
        if invert:
            mark = " ✅B优" if diff < 0 else (" ✅A优" if diff > 0 else "")
        print(f"{label:<22}{va:>15}{unit}{vb:>15}{unit}{diff:>+13.4f}{unit}{mark}")

    row("答案可用率", "useful_rate", "useful_rate", "")
    row("有引用比例", "citation_rate", "citation_rate", "")
    row("平均答案长度", "avg_answer_len", "avg_answer_len", "字")
    row("平均耗时", "avg_elapsed_sec", "avg_elapsed_sec", "s", invert=True)
    row("单次成本", "avg_cost_yuan", "avg_cost_yuan", "¥", invert=True)
    row("总成本", "total_cost_yuan", "total_cost_yuan", "¥", invert=True)
    row("LLM 调用总次数", "total_llm_calls", "total_llm_calls", "次", invert=True)

    print("-" * 68)
    print(f"{'平均轮数':<22}{'—':>16}{B['avg_rounds']:>16}")
    print(f"{'平均工具调用':<22}{'—':>16}{B['avg_tool_calls']:>16}")
    print(f"{'finish 完成率':<22}{'—':>16}{B['finished_rate']:>16}")
    print(f"{'降级次数':<22}{'0':>16}{B['fallback_count']:>16}")
    print(f"{'A 组异常次数':<22}{A['errors']:>16}{'—':>16}")

    print("\n【Agent 行为归因分布】")
    if tag_count:
        for t, c in sorted(tag_count.items(), key=lambda x: -x[1]):
            print(f"  {t:<18} {c}/{n}")
    else:
        print("  无异常 —— 全部样本都走完了标准路径")

    print("\n【Agent 表现更差的样本】")
    if worse:
        for w in worse:
            print(f"  · {w['mail_id']} {w['subject'][:34]}")
            print(f"    原因：{w['reason']} | A ¥{w['A_cost']:.5f} vs B ¥{w['B_cost']:.5f}")
            if w["tags"]:
                print(f"    归因：{', '.join(w['tags'])}")
            if w["fallback_reason"]:
                print(f"    降级：{w['fallback_reason'][:60]}")
    else:
        print("  无 —— Agent 未在任何样本上明显更差")

    print(f"\n落盘：{OUT_PATH}")


if __name__ == "__main__":
    main()
