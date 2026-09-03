# -*- coding: utf-8 -*-
"""
询盘响应工作流（状态机 + 人工介入 + RAG）
=========================================
把「收到一封询盘邮件」从单步处理升级为多步可编排、可追踪、可卡控的业务流。

为什么需要工作流（直接命中 JD 的「工作流」要求）：
    单封处理（P0 的 agent.py）只解决"这封邮件是什么/有什么字段"。
    真实业务要的是跨时间的多步编排：
        收到询盘 → 查历史报价(RAG) → 起草回复 → 等人工审核 → 超时升级 → 发出
    每一步都有状态、责任人和时效（SLA），任何一步卡住都要能追溯、能升级、能介入。

状态机：
    NEW ──auto──> RETRIEVING ──auto──> DRAFTING ──auto──> PENDING_REVIEW
    PENDING_REVIEW ─(approve, 人工)──> APPROVED ──auto──> SENT
    PENDING_REVIEW ─(reject, 人工)──> REJECTED ──auto──> DRAFTING (打回重做)
    PENDING_REVIEW ─(SLA 超时, tick)──> ESCALATED
    ESCALATED ─(intervene, 经理)──> PENDING_REVIEW (重新打开)

设计要点：
    - 所有状态迁移都过 transit()，带守卫校验 + 审计轨迹（谁、何时、为什么）
    - RAG 检索作为独立状态（RETRIEVING），可观测、可替换（明天换向量检索只需换 retriever）
    - SLA 用 tick(now) 推进时间，demo 可加速；真实环境由定时任务驱动
    - 人工介入是显式节点（PENDING_REVIEW），支持 approve / reject / edit 三种决策

作者：麦当
日期：2026-09-01
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vector_retriever import build_hybrid
from context_builder import build_context, EMPTY_CONTEXT

# 生成端（RAG 的 G）：真正的 LLM 起草，替代原先的模板拼接
# 延迟导入 —— 无 DeepSeek Key 时仍能跑通工作流的其余部分
try:
    from generator import AnswerGenerator, _template_fallback
except Exception:
    AnswerGenerator = None
    _template_fallback = None

# 状态常量
S_NEW = "NEW"
S_RETRIEVING = "RETRIEVING"
S_DRAFTING = "DRAFTING"
S_PENDING_REVIEW = "PENDING_REVIEW"
S_APPROVED = "APPROVED"
S_SENT = "SENT"
S_REJECTED = "REJECTED"
S_ESCALATED = "ESCALATED"

# 合法迁移表（守卫）： from -> {to: 允许的动作/触发}
_TRANSITIONS = {
    S_NEW: {S_RETRIEVING: "auto"},
    S_RETRIEVING: {S_DRAFTING: "auto"},
    S_DRAFTING: {S_PENDING_REVIEW: "auto"},
    S_PENDING_REVIEW: {S_APPROVED: "approve", S_REJECTED: "reject",
                       S_ESCALATED: "timeout"},
    S_REJECTED: {S_DRAFTING: "rework"},
    S_APPROVED: {S_SENT: "auto"},
    S_ESCALATED: {S_PENDING_REVIEW: "intervene"},
}

SLA_HOURS = 24  # PENDING_REVIEW 的人工审核 SLA


def _now():
    return datetime.now()


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


class WorkflowCase:
    """一个询盘的生命周期状态机"""

    def __init__(self, mail_result, retriever=None, sla_hours=SLA_HOURS, clock=None,
                 generator=None, use_llm_draft=True):
        """
        mail_result:   agent.process_one() 的输出（含 category/fields/priority 等）
        retriever:     混合检索器实例（BM25+向量，默认加载语料）
        generator:     AnswerGenerator 实例（None 时自动创建；传 False 可强制走模板）
        use_llm_draft: 是否启用 LLM 起草（关掉即退回旧的模板拼接路径）
        """
        self.result = mail_result
        self.fields = mail_result.get("fields", {})
        self.state = S_NEW
        self.history = []          # 审计轨迹
        self.retrieved = []        # RAG 命中
        self.draft = ""
        # —— 生成端（Day05 新增）——
        self.context_bundle = None  # 上下文组装结果（含 id_map）
        self.cited_ids = []         # 答案引用到的 chunk id（审计链：答案 → 来源）
        self.gen_mode = None        # "llm" / "template_fallback"
        self.gen_result = None      # generator 的原始返回（含耗时/成本/无效引用）
        self._use_llm_draft = use_llm_draft
        if generator is None:
            self._generator = AnswerGenerator() if (use_llm_draft and AnswerGenerator) else None
        else:
            self._generator = generator
        self.review_decision = None
        self.sent_at = None
        self.escalated_at = None
        self.sla_hours = sla_hours
        self._clock = clock or _now
        self._retriever = retriever or build_hybrid()[0]
        self._entered_at = {S_NEW: self._clock()}

    # ------------------------------------------------------------------
    # 状态迁移（带守卫 + 审计）
    # ------------------------------------------------------------------
    def transit(self, to_state, actor="system", note=""):
        allowed = _TRANSITIONS.get(self.state, {})
        trigger = allowed.get(to_state)
        if trigger is None:
            raise ValueError(
                f"非法迁移：{self.state} → {to_state}（合法目标：{list(allowed) or '无'}）")
        now = self._clock()
        self.history.append({
            "from": self.state, "to": to_state, "actor": actor,
            "note": note or f"触发:{trigger}", "at": _fmt(now),
        })
        self.state = to_state
        self._entered_at[to_state] = now
        if to_state == S_ESCALATED:
            self.escalated_at = now
        return self

    # ------------------------------------------------------------------
    # 自动管线：NEW → RETRIEVING → DRAFTING → PENDING_REVIEW
    # ------------------------------------------------------------------
    def run_pipeline(self):
        if self.state != S_NEW:
            raise RuntimeError(f"run_pipeline 只能从 NEW 启动，当前 {self.state}")
        self._step_retrieve()
        self._step_draft()
        self.transit(S_PENDING_REVIEW, actor="system",
                     note=f"起草完成，等待人工审核（SLA {self.sla_hours}h）")
        return self

    def _step_retrieve(self):
        self.transit(S_RETRIEVING, actor="system", note="进入 RAG 检索：查历史报价/客户往来")
        cust = self.fields.get("customer") or ""
        prod = self.fields.get("product") or ""
        query = f"{cust} {prod}".strip() or self.result.get("subject", "")
        hits = self._retriever.search(query, top_k=3)
        self.retrieved = hits
        return self

    def _step_draft(self):
        """RETRIEVING → DRAFTING：组装上下文后起草。"""
        self.transit(S_DRAFTING, actor="system", note="基于提取字段 + RAG 上下文起草回复")

        # —— 组装上下文（带 [n] 编号 + id_map）——
        cust = self.fields.get("customer") or ""
        prod = self.fields.get("product") or ""
        query = f"{cust} {prod}".strip() or self.result.get("subject", "")
        self.context_bundle = build_context(query, self.retrieved)
        return self._draft_from_context()

    def _draft_from_context(self):
        """基于已组装好的上下文起草：优先 LLM 生成（带引用标注），失败降级模板。

        拆成独立方法的原因：打回重做（reject）时状态已经迁到 DRAFTING，
        不能再 transit 一次，只需复用同一个 context_bundle 重新生成答案。

        审计价值：self.cited_ids 记录「答案引用了哪些 chunk」，
        配合 context_bundle["id_map"] 可回溯到原文，构成完整可验证链路。
        """
        bundle = self.context_bundle
        query = bundle["query"] if bundle else ""
        id_map = bundle.get("id_map", {}) if bundle else {}

        if not self._generator:
            # 强制模板路径：使用带 [n] 引用的新版模板
            res = _template_fallback(query, bundle["context"] if bundle else "", id_map, "no_generator")
            self.draft = res["answer"]
            self.gen_mode = "template_fallback"
            self.cited_ids = res["cited_ids"]
            self.gen_result = res
            self.transit(S_PENDING_REVIEW, actor="system",
                         note=f"起草完成（模板路径，无 LLM），等待人工审核（SLA {self.sla_hours}h）")
            return self

        # —— LLM 生成 ——
        res = self._generator.generate(query, bundle["context"], id_map)
        self.gen_mode = res["mode"]
        if res["mode"] == "llm" and res["answer"]:
            self.draft = (
                f"{res['answer']}\n\n"
                f"---\n"
                f"[生成方式] LLM（引用 {len(res['cited_ids'])} 个历史片段："
                f"{', '.join(res['cited_ids']) or '无'}）\n"
                f"[待人工确认] 单价 / 交期 / 付款方式 / 报价有效期"
            )
            self.cited_ids = res["cited_ids"]
        else:
            # 降级：保留模板产出，但显式标注，不冒充生成结果
            self.draft = res["answer"]
            self.cited_ids = res.get("cited_ids", [])
        self.gen_result = res
        self.transit(S_PENDING_REVIEW, actor="system",
                     note=f"起草完成（{self.gen_mode}），等待人工审核（SLA {self.sla_hours}h）")
        return self

    def _render_draft_template(self):
        f = self.fields
        cust = f.get("customer") or "Dear Customer"
        prod = f.get("product") or "the products"
        qty = f.get("quantity") or "TBD"
        price = f.get("price") or "TBD（按历史成交价参考）"
        deadline = f.get("deadline") or "TBD"

        # 用 RAG 命中的历史记录生成"参考报价"提示
        # 关键：只注入 score>0 的相关命中；全 0 分（客户/产品不在语料）说明无历史，
        # 此时必须显式标注"无相关历史"，绝不能把无关客户的历史塞进草稿（数据污染）。
        ref_lines = []
        for h in self.retrieved:
            if h.get("score", 0) > 0:
                ref_lines.append(f"  · {h['customer']}（{h['id']}）：{h['text']}")
        ref_block = "\n".join(ref_lines) if ref_lines else (
            "  · 无相关历史记录（建议人工新建客户档案，或接入更大知识库）")

        return (
            f"Subject: Re: Quotation for {prod}\n\n"
            f"Dear {cust},\n\n"
            f"Thank you for your inquiry on {prod}.\n\n"
            f"【提取信息】数量 {qty} ｜ 目标价 {price} ｜ 期望交期 {deadline}\n\n"
            f"【历史参考（RAG 检索）】\n{ref_block}\n\n"
            f"We are preparing the formal PI and will follow up shortly.\n\n"
            f"Best regards,\nSales Team\n"
            f"---\n"
            f"[待人工确认] 单价 / 交期 / 付款方式 / 报价有效期"
        )

    # ------------------------------------------------------------------
    # 时间推进：SLA 超时升级
    # ------------------------------------------------------------------
    def tick(self, now=None):
        now = now or self._clock()
        if self.state == S_PENDING_REVIEW:
            entered = self._entered_at.get(S_PENDING_REVIEW)
            if entered and (now - entered) > timedelta(hours=self.sla_hours):
                self.transit(S_ESCALATED, actor="scheduler",
                             note=f"审核超 {self.sla_hours}h 未处理，自动升级经理")
        return self

    # ------------------------------------------------------------------
    # 人工介入
    # ------------------------------------------------------------------
    def review(self, decision, actor="human", edited_draft=None, note=""):
        """
        decision: 'approve' | 'reject' | 'edit'
        - approve：进入 APPROVED（可随后 send）
        - reject：打回重做（REJECTED → DRAFTING）
        - edit：用 edited_draft 覆盖草稿后 approve
        """
        if self.state != S_PENDING_REVIEW:
            raise RuntimeError(f"review 只能在 PENDING_REVIEW 调用，当前 {self.state}")
        self.review_decision = decision
        if decision == "edit" and edited_draft:
            self.draft = edited_draft
        if decision in ("approve", "edit"):
            self.transit(S_APPROVED, actor=actor,
                         note=note or f"人工审核通过（{decision}）")
        elif decision == "reject":
            self.transit(S_REJECTED, actor=actor, note=note or "人工驳回，打回重做")
            self.transit(S_DRAFTING, actor="system", note="根据驳回意见重新起草")
            # 重做沿用同一个上下文（含引用编号与 id_map），只是重新生成一次答案
            self._draft_from_context()
        else:
            raise ValueError(f"未知决策：{decision}")
        return self

    def intervene(self, actor="manager", note="经理介入，重新分配审核"):
        if self.state != S_ESCALATED:
            raise RuntimeError(f"intervene 只能在 ESCALATED 调用，当前 {self.state}")
        self.transit(S_PENDING_REVIEW, actor=actor, note=note)
        return self

    def resubmit(self, actor="system", note="打回重做完成，重新提交人工审核"):
        """REJECTED→DRAFTING 之后，把重做稿重新提交审核（DRAFTING→PENDING_REVIEW）"""
        if self.state != S_DRAFTING:
            raise RuntimeError(f"resubmit 只能在 DRAFTING 调用，当前 {self.state}")
        self.transit(S_PENDING_REVIEW, actor=actor, note=note)
        return self

    def send(self, mail_sender=None):
        if self.state != S_APPROVED:
            raise RuntimeError(f"send 只能在 APPROVED 调用，当前 {self.state}")
        now = self._clock()
        self.sent_at = now
        self.transit(S_SENT, actor="system", note="回复已发送（接 mail_connector 即真实发信）")
        return self

    # ------------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------------
    def summary(self):
        return {
            "case_id": self.result.get("id"),
            "category": self.result.get("category"),
            "priority": self.result.get("priority"),
            "state": self.state,
            "retrieved_top": [h["id"] for h in self.retrieved[:3]],
            # —— 生成端可审计信息（Day05 新增）——
            "gen_mode": self.gen_mode,          # llm = 真生成；template_fallback = 降级
            "cited_ids": self.cited_ids,        # 答案 → 来源 chunk，可回溯核对
            "gen_cost_yuan": (self.gen_result or {}).get("est_cost"),
            "review_decision": self.review_decision,
            "sent_at": _fmt(self.sent_at) if self.sent_at else None,
            "history": self.history,
            "draft": self.draft,
        }


if __name__ == "__main__":
    import json
    from agent import MailAgent

    BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    emails = json.load(open(os.path.join(BASE, "data", "e2e_emails.json"), encoding="utf-8"))
    inquiry = next(e for e in emails if e.get("category") == "inquiry")
    res = MailAgent().process_one(inquiry)

    case = WorkflowCase(res)
    case.run_pipeline()
    print("状态:", case.state)
    print("RAG 命中:", [h["id"] for h in case.retrieved])
    print("草稿:\n", case.draft)
    case.review("approve")
    case.send()
    print("最终状态:", case.state)
    print("审计轨迹:")
    for h in case.history:
        print(f"  {h['at']} {h['actor']:>8} {h['from']}→{h['to']}  {h['note']}")
