# -*- coding: utf-8 -*-
"""
多智能体（JD #2）：Supervisor 路由 + Specialist 执行
===================================================

为什么做：Day06 用 A/B + H3 证明「确定型邮件回复」用固定流水线（A3）最划算，
自主单 Agent 零质量收益、6.4 倍成本。但 JD 明确要求展示「多智能体」能力——
它的价值在**任务异质、需要不同专业**的场景：询盘要报价知识、客诉要共情与补偿规则、
通知几乎不用检索。本模块用「主管路由 + 专员执行」模式落地，复用同一套 tools 与生成器。

架构
----
    Supervisor（主管）  : classify → handoff 委派 → finish 汇总专员回复
    Specialist（专员）  : 4 类，各自一套领域 SYSTEM_PROMPT，跑和 Agent 相同的 Function Calling 循环
    handoff（主管的工具）: 调用即触发对应专员 AgentLoop.run()，返回其答案

主管不直接写回复，只做路由；专员各自用领域提示独立完成任务。这是真实的 supervisor-worker 多智能体。

零新增依赖（requests 已是项目既有依赖）。

作者：麦当 · 2026-09-04（Day06 续 · 多智能体）
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tools import ToolSession, make_tools, execute_tool, TOOLS  # noqa: E402
from agent_loop import AgentLoop  # noqa: E402

SUPERVISOR_PROMPT = """你是外贸邮件处理系统的调度主管（Supervisor）。
你不直接起草客户回复，而是判断邮件类型并把任务委派给对应专员（Specialist）：
- inquiry（询盘）：产品/价格/规格咨询 → handoff 给 inquiry_specialist
- order（订单）：下单/改单/交付 → handoff 给 order_specialist
- complaint（投诉）：质量/货损/索赔 → handoff 给 complaint_specialist
- notification（通知）：付款/发货/清关 → handoff 给 notification_specialist

步骤：
1. 调 classify_email 判断类型（必要时 extract_fields 取要素）。
2. 调 handoff 委派给对应专员，专员会独立完成检索与起草并返回回复正文。
3. 收到专员回复后，调 finish 提交（答案必须原样等于专员的回复正文，不要改写业务内容）。

成本意识：不要重复调用工具；类型明确就直接 handoff。
"""

SPECIALIST_PROMPTS = {
    "inquiry_specialist": (
        "你是询盘专员。基于检索到的客户历史，起草专业的询盘回复：聚焦产品规格、价格、交期、"
        "MOQ、样品政策。事实必须检索并标注 [n]；无历史成交价时承诺跟进，不要编造。"
    ),
    "order_specialist": (
        "你是订单专员。聚焦确认订单细节、交付安排、变更与追加数量、付款与单据。"
        "核对历史成交的规格/价格并标注 [n]；与本次邮件有出入时显式提示客户确认。"
    ),
    "complaint_specialist": (
        "你是客诉专员。先共情、再定性（质量/货损/短缺/延误）、给出可执行的补偿或处理方案，"
        "并标注依据 [n]。语气专业且承担责任，不推诿。"
    ),
    "notification_specialist": (
        "你是通知专员。付款/发货/清关类通知通常无需检索历史，确认收到并给简要下一步即可；"
        "若通知里隐含问题（如清关异常、延误），再视情况检索。不要硬检索无关信息。"
    ),
}

HANDOFF_TOOL = {
    "type": "function",
    "function": {
        "name": "handoff",
        "description": (
            "把当前邮件委派给对应专员处理，返回专员起草好的客户回复正文。"
            "【何时用】已经用 classify_email 判断出邮件类型后，立即调用。"
            "参数 to 必须是 inquiry_specialist / order_specialist / complaint_specialist / "
            "notification_specialist 之一。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "目标专员名"},
                "mail_id": {"type": "string", "description": "邮件 ID，便于专员定位上下文"},
            },
            "required": ["to", "mail_id"],
        },
    },
}


class MultiAgent:
    """Supervisor 路由 + Specialist 执行的多智能体编排。"""

    def __init__(self, max_rounds=10, timeout=30, verbose=True):
        self.max_rounds = max_rounds
        self.timeout = timeout
        self.verbose = verbose
        self._last = {}

    def _handoff_impl(self, to, mail_id):
        if to not in SPECIALIST_PROMPTS:
            return json.dumps({"error": f"未知专员：{to}"}, ensure_ascii=False)
        sub = AgentLoop(max_rounds=self.max_rounds, timeout=self.timeout, verbose=False)
        res = sub.run(self._mail, system_prompt=SPECIALIST_PROMPTS[to])
        out = {
            "delegated_to": to,
            "answer": res.get("answer", ""),
            "cited_ids": res.get("cited_ids", []),
            "specialist_mode": res.get("mode"),
            "specialist_rounds": res.get("rounds"),
            "specialist_fallback": res.get("fallback_reason"),
        }
        self._last = out
        return json.dumps(out, ensure_ascii=False)

    def run(self, mail):
        t0 = time.time()
        self._mail = mail
        self._last = {}
        session = ToolSession(mail)
        _, base_impl = make_tools(session)
        impl = dict(base_impl)
        impl["handoff"] = lambda **kw: self._handoff_impl(**kw)
        supervisor_tools = TOOLS + [HANDOFF_TOOL]

        loop = AgentLoop(max_rounds=self.max_rounds, timeout=self.timeout, verbose=self.verbose)
        res = loop.run(
            mail,
            system_prompt=SUPERVISOR_PROMPT,
            tools_spec=supervisor_tools,
            impl=impl,
        )

        # 透传：主管 finish 的答案应是专员回复；若主管没原样透传，用专员答案兜底
        if not (res.get("answer") or "").strip() and self._last.get("answer"):
            res["answer"] = self._last["answer"]
        if not res.get("cited_ids") and self._last.get("cited_ids"):
            res["cited_ids"] = self._last["cited_ids"]
        res["delegated_to"] = self._last.get("delegated_to")
        res["mode"] = "multi_agent"
        res["elapsed_sec"] = round(time.time() - t0, 3)
        return res


def main():
    import argparse as _ap
    import json as _json

    _ap_ = _ap.ArgumentParser(description="多智能体：主管路由 + 专员执行（演示）")
    _ap_.add_argument("--rounds", type=int, default=10)
    _ap_.add_argument("--limit", type=int, default=4, help="每类取 1 封做演示（默认 4 封）")
    args = _ap_.parse_args()

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(base, "data", "e2e_emails.json"), encoding="utf-8") as f:
        emails = _json.load(f)

    seen, demo = set(), []
    for e in emails:
        c = e.get("category")
        if c not in seen:
            seen.add(c)
            demo.append(e)
        if len(demo) >= args.limit:
            break

    ma = MultiAgent(max_rounds=args.rounds, verbose=True)
    for e in demo:
        print("\n" + "=" * 60)
        print(f"邮件 {e.get('id')} | {e.get('subject')}")
        res = ma.run(e)
        print(f"委派 → {res.get('delegated_to')} | 模式 {res.get('mode')} | "
              f"finish {res.get('finished')} | ¥{res.get('cost_yuan'):.5f} | {res.get('rounds')}轮")
        print("答案(前 320 字):\n", (res.get("answer") or "")[:320])


if __name__ == "__main__":
    main()
