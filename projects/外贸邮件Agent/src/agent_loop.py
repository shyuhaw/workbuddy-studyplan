# -*- coding: utf-8 -*-
"""
Agent 执行循环（Function Calling）
=================================
Day06 的核心：把「固定流水线」升级为「模型自主决策的 Agent」。

流水线 vs Agent 的本质区别
--------------------------
    workflow.py::run_pipeline()  顺序由人写死 → _step_retrieve() → _step_draft()
    agent_loop.py::run()         顺序由模型决定 → 它看着工具清单自己选

循环结构（这是面试要讲清的那张图）
--------------------------------
    ① 你发给模型：消息历史 + 工具清单
    ② 模型决策：直接回答（结束）or 返回一堆 tool_calls
    ③ 你执行：跑本地 Python 函数（模型不执行代码，只出决策）
    ④ 你回填：append {"role":"tool", "tool_call_id": id, "content": 结果}
    ⑤ 回到 ①，直到模型不再要工具 / 调了 finish / 超过 max_rounds

四条硬约束（缺一不可）
----------------------
1. **幂等**：同一 (工具, 参数) 二次调用返回缓存，不重复烧钱
2. **max_rounds 兜底**：模型可能死循环，必须有轮数上限
3. **降级**：任何异常 / 超轮 / 未按规矩调 finish → 回退固定流水线，不崩
4. **轨迹留痕**：每一步都记下来，这是 A/B 归因和优化的唯一依据

关于"为什么明知可能更差还要做"
------------------------------
固定流水线的 7 步顺序是调了 5 天调出来的。让模型自己选，很可能漏步骤 / 多调一次 / 顺序错。
**这是有意为之**：JD 高频句是「理解大模型能力边界与工程取舍」，
「我实测了 A vs B，结论是步骤确定的流程用流水线」—— 这句话比"我会 Function Calling"值钱得多。

零新增依赖（requests 已是项目既有依赖）。

作者：麦当
日期：2026-09-04（Day06）
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import ToolSession, make_tools, execute_tool, TOOLS  # noqa: E402

try:
    from llm_fallback import DeepSeekProvider, FallbackManager
except Exception:
    DeepSeekProvider = None
    FallbackManager = None

# 计价与 generator.py 保持一致（DeepSeek 官方 2026-09）
PRICE_IN_PER_TOKEN = 1.0 / 1_000_000
PRICE_OUT_PER_TOKEN = 8.0 / 1_000_000

SYSTEM_PROMPT = """你是外贸业务 Agent。你的任务是处理一封客户邮件，产出给客户的回复草稿。

你有 7 个工具可用，调用顺序和次数由你自己决定。

硬性规则（违反即任务失败）：

1. **事实必须检索**：回复中一旦涉及价格、规格、交期、历史往来等事实性信息，
   必须先 retrieve_history 检索，再 build_context 组装，再 generate_answer 生成。
   不检索就回答 = 编造，这是最严重的错误。

2. **生成前必须组装**：generate_answer 依赖 build_context 产出的带编号上下文。
   没有编号，答案就无法标注引用，也就无法验证它有没有编。

3. **无依据就直说**：上下文里没有的信息，直接回答「依据现有记录无法确认」，不要推测、不要编。

4. **必须调 finish 结束**：完成后一定要调用 finish 提交最终答案。
   不调 finish 视为任务未完成。

5. **不要凑步骤**：如果邮件不涉及任何历史信息（例如纯催进度、纯礼貌回复），
   可以跳过检索直接 finish。**无效的工具调用是纯成本。**

成本意识：每次工具调用都消耗 token。相同参数的重复调用不会重复执行（系统已做幂等），
但仍会消耗 token，所以想清楚再调。
"""

# 启用跨轮记忆时，追加到系统提示后的指令
MEMORY_INSTRUCTION = """

跨轮记忆（本邮件处理前已加载该客户历史上下文）：
- 开始处理前：先调 recall_memory 拉取该客户的历史记忆，保证回复连贯（续单/跟进/反复问题）。
- 处理完、确认了值得跨轮复用的信息后：调 remember_fact 存入（合同规格、约定价、偏好、未决问题、结论）。
- 重要边界：记忆是工作笔记，可能过时或出错；涉及价格/规格等权威事实，正式答复前仍必须
  retrieve_history 校验，不得拿记忆当权威依据。记忆与本次检索冲突时以检索为准。
"""

USER_TEMPLATE = """请处理这封客户邮件，产出给客户的回复草稿。

【邮件ID】{mail_id}
【发件人】{sender}
【主题】{subject}

【正文】
{body}

完成后请调用 finish 提交最终答案。"""


def _calc_cost(usage):
    """按 DeepSeek 官价折算人民币"""
    pin = usage.get("prompt_tokens", 0) or 0
    pout = usage.get("completion_tokens", 0) or 0
    return pin * PRICE_IN_PER_TOKEN + pout * PRICE_OUT_PER_TOKEN


class AgentLoop:
    """Function Calling 执行循环"""

    def __init__(self, provider=None, max_rounds=6, timeout=30, verbose=True):
        self.provider = provider
        self.max_rounds = max_rounds
        self.timeout = timeout
        self.verbose = verbose

    def _get_provider(self):
        if self.provider is not None:
            return self.provider
        if FallbackManager is None:
            return None
        cfg = FallbackManager._load_config()
        key = cfg.get("deepseek", {}).get("api_key") or os.environ.get("DEEPSEEK_API_KEY")
        if not key or DeepSeekProvider is None:
            return None
        self.provider = DeepSeekProvider(key, timeout=self.timeout)
        return self.provider

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self, mail, task=None, system_prompt=None, tools_spec=None, impl=None, memory_store=None):
        """跑一次 Agent，返回结构化结果（永不抛异常）。

        返回字段：
            mode            "agent" | "pipeline_fallback"
            answer          最终答案（降级时为流水线草稿）
            cited_ids       引用到的 chunk id
            finished        模型是否主动调了 finish
            rounds          实际轮数
            tool_calls      工具调用次数
            trace           每步轨迹（归因依据）
            fallback_reason 降级原因（None 表示未降级）
            elapsed_sec / cost_yuan / usage
        """
        t0 = time.time()
        session = ToolSession(mail, memory=memory_store)
        if tools_spec is None or impl is None:
            tools_spec, impl = make_tools(session, memory=memory_store)
        provider = self._get_provider()

        usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
        cost = 0.0

        # provider 不可用 → 直接降级，不浪费时间
        if provider is None or not hasattr(provider, "chat"):
            return self._fallback(mail, session, "provider 不可用（无 API Key 或不支持 Function Calling）",
                                  elapsed=time.time() - t0)

        # 跨轮记忆注入：系统提示追加记忆指令；用户消息预填该客户历史上下文
        sys_content = system_prompt or SYSTEM_PROMPT
        user_content = task or USER_TEMPLATE.format(
            mail_id=mail.get("id", ""),
            sender=mail.get("from", ""),
            subject=mail.get("subject", ""),
            body=mail.get("body", ""),
        )
        if memory_store is not None:
            sys_content = sys_content + MEMORY_INSTRUCTION
            cust = (mail.get("expected", {}) or {}).get("customer") or mail.get("customer")
            if cust:
                related = memory_store.recall(customer=cust, limit=5)
                if related:
                    block = (
                        "\n\n【该客户跨轮记忆（仅供参考，非权威事实；与本次冲突以检索为准）】\n"
                        + "\n".join(
                            f"- [{e['fact_type']}] {e['fact']}（来源:{e['mail_id'] or '—'}）"
                            for e in related
                        )
                    )
                    user_content = user_content + block

        messages = [
            {"role": "system", "content": sys_content},
            {"role": "user", "content": user_content},
        ]

        finished = False
        final_answer = ""
        final_cited = []
        rounds = 0
        n_calls = 0
        stop_reason = None

        try:
            for rnd in range(1, self.max_rounds + 1):
                rounds = rnd
                message, usage = provider.chat(messages=messages, tools=tools_spec)

                for k in usage_total:
                    usage_total[k] += usage.get(k, 0) or 0
                cost += _calc_cost(usage)

                calls = message.get("tool_calls") or []

                # —— 终止条件一：模型直接回答，不再要工具 ——
                if not calls:
                    stop_reason = "模型未调用工具直接返回文本（未调 finish）"
                    break

                # assistant 消息必须原样回填，模型才知道自己说过要调哪些工具
                messages.append({
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": calls,
                })

                for c in calls:
                    fn = (c.get("function") or {})
                    name = fn.get("name")
                    raw_args = fn.get("arguments")
                    call_id = c.get("id")
                    n_calls += 1

                    if self.verbose:
                        print(f"  [R{rnd}] {name}({(raw_args or '')[:80]})")

                    out = execute_tool(name, raw_args, impl, session)
                    messages.append({"role": "tool", "tool_call_id": call_id,
                                     "content": out})

                    if name == "finish":
                        try:
                            data = json.loads(out)
                            final_answer = data.get("answer") or ""
                            final_cited = data.get("cited_ids") or []
                        except Exception:
                            final_answer = out
                        finished = True
                        stop_reason = "finish"
                        break

                if finished:
                    break

                # —— 终止条件二：超轮 ——
                if rnd == self.max_rounds:
                    stop_reason = f"达到 max_rounds={self.max_rounds} 仍未 finish"
        except Exception as e:
            stop_reason = f"循环异常：{type(e).__name__}: {e}"

        # —— 未按规矩结束 → 降级 ——
        if not (finished and final_answer.strip()):
            reason = stop_reason or "未知原因未产出答案"
            if finished and not final_answer.strip():
                reason = "finish 提交了空答案"
            return self._fallback(mail, session, reason, elapsed=time.time() - t0,
                                  rounds=rounds, n_calls=n_calls, cost=cost,
                                  usage=usage_total)

        return {
            "mode": "agent",
            "answer": final_answer,
            "cited_ids": final_cited,
            "finished": True,
            "stop_reason": stop_reason,
            "rounds": rounds,
            "tool_calls": n_calls,
            "trace": session.trace,
            "fallback_reason": None,
            "elapsed_sec": round(time.time() - t0, 3),
            "cost_yuan": round(cost + session.llm_cost, 6),
            "usage": usage_total,
        }

    # ------------------------------------------------------------------
    # 降级：回退到 Day01-05 的固定流水线
    # ------------------------------------------------------------------
    def _fallback(self, mail, session, reason, elapsed=0.0, rounds=0,
                  n_calls=0, cost=0.0, usage=None):
        """降级不是掩盖，是兜底。降级原因会被完整记录，A/B 评测里单独计数。"""
        answer, cited, gen_mode = "", [], None
        try:
            from agent import MailAgent
            from workflow import WorkflowCase
            res = MailAgent().process_one(mail)
            case = WorkflowCase(res).run_pipeline()
            answer = case.draft or ""
            cited = case.cited_ids or []
            gen_mode = case.gen_mode
        except Exception as e:
            answer = f"[降级失败] {type(e).__name__}: {e}"
            reason = f"{reason}；且流水线降级也失败：{e}"

        return {
            "mode": "pipeline_fallback",
            "answer": answer,
            "cited_ids": cited,
            "gen_mode": gen_mode,
            "finished": False,
            "stop_reason": reason,
            "fallback_reason": reason,
            "rounds": rounds,
            "tool_calls": n_calls,
            "trace": session.trace,
            "elapsed_sec": round(elapsed, 3),
            "cost_yuan": round(cost + session.llm_cost, 6),
            "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
        }


def _demo_mail():
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(base, "data", "e2e_emails.json"), encoding="utf-8") as f:
        emails = json.load(f)
    return next(e for e in emails if e.get("category") == "inquiry")


def main():
    ap = argparse.ArgumentParser(description="Agent 执行循环（Function Calling）")
    ap.add_argument("--rounds", type=int, default=6, help="最大轮数，默认 6")
    ap.add_argument("--quiet", action="store_true", help="不打印逐轮轨迹")
    args = ap.parse_args()

    mail = _demo_mail()
    print(f"演示邮件：{mail.get('id')} | {mail.get('subject')}\n")

    loop = AgentLoop(max_rounds=args.rounds, verbose=not args.quiet)
    res = loop.run(mail)

    print("\n" + "—" * 60)
    print(f"模式        : {res['mode']}")
    print(f"是否 finish : {res['finished']}")
    print(f"轮数        : {res['rounds']} / 上限 {args.rounds}")
    print(f"工具调用    : {res['tool_calls']} 次")
    print(f"引用 chunk  : {res['cited_ids']}")
    print(f"终止原因    : {res['stop_reason']}")
    if res["fallback_reason"]:
        print(f"⚠️ 降级原因   : {res['fallback_reason']}")
    print(f"耗时        : {res['elapsed_sec']}s")
    print(f"成本        : ¥{res['cost_yuan']:.6f}")
    print(f"Token       : {res['usage']}")

    print("\n【工具调用轨迹】")
    for i, t in enumerate(res["trace"], 1):
        flag = "（缓存）" if t.get("cached") else ""
        status = "✅" if t["ok"] else "❌"
        print(f"  {i}. {status} {t['tool']} {flag} {t['elapsed_ms']}ms "
              f"→ {t['result_len']}字符")

    print("\n【最终答案】")
    print(res["answer"][:600])


if __name__ == "__main__":
    main()
