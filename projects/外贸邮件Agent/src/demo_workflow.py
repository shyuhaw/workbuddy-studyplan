# -*- coding: utf-8 -*-
"""
询盘响应工作流 · 可运行 Demo
============================
展示：分类提取 → 工作流状态机（NEW→…→PENDING_REVIEW）→ RAG 检索 → 起草
      → 人工审核通过 → 发送；
并演示 SLA 超时自动升级 + 经理介入 的异常分支。

用法：
    python src/demo_workflow.py
输出：状态迁移轨迹 + RAG 命中（带 BM25 得分）+ 起草草稿 + 审计日志
落盘：output/workflow_demo.json

作者：麦当
日期：2026-09-01
"""

import os
import sys
import json
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent import MailAgent
from workflow import WorkflowCase, S_PENDING_REVIEW

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE_DIR, "data", "e2e_emails.json")
OUT = os.path.join(BASE_DIR, "output", "workflow_demo.json")

SEP = "=" * 82


def banner(t):
    print(f"\n{SEP}\n{t}\n{SEP}")


def show_case(case, title):
    banner(title)
    s = case.summary()
    print(f"Case {s['case_id']} ｜ 分类 {s['category']} ｜ 优先级 {s['priority']} ｜ 状态 {s['state']}")
    print("\n[RAG 检索命中]")
    if not case.retrieved:
        print("  （无）")
    for h in case.retrieved:
        print(f"  [{h.get('score'):.3f}] {h['id']} {h['customer']}: {h['text'][:64]}...")
    if case.draft:
        print("\n[起草草稿]")
        print(case.draft)
    print("\n[审计轨迹]")
    for h in case.history:
        print(f"  {h['at']}  {h['actor']:>9}  {h['from']}→{h['to']}  {h['note']}")


def main():
    emails = json.load(open(DATA, "r", encoding="utf-8"))
    agent = MailAgent()
    inquiry = next(e for e in emails if e.get("category") == "inquiry")
    res = agent.process_one(inquiry)
    print(SEP)
    print("询盘响应工作流 Demo")
    print(SEP)
    print(f"输入邮件: {res['id']} ｜ {res['subject']}")
    print(f"分类: {res['category']}（{res['cat_source']}）｜ 优先级: {res['priority']}")

    # ---------------- 主线：正常通过 ----------------
    case = WorkflowCase(res)
    case.run_pipeline()
    case.review("approve", actor="业务员小李", note="价格沿用历史成交价，通过")
    case.send()
    show_case(case, "主线：询盘 → 检索 → 起草 → 人工通过 → 发送")

    # ---------------- 异常分支：SLA 超时升级 ----------------
    inquiry2 = next(e for e in emails if e.get("category") == "inquiry" and e.get("id") != res["id"])
    res2 = agent.process_one(inquiry2)
    case2 = WorkflowCase(res2)
    case2.run_pipeline()
    # 人为把进入 PENDING_REVIEW 的时间推前 25h，模拟超时
    past = datetime.now() - timedelta(hours=25)
    case2._entered_at[S_PENDING_REVIEW] = past
    case2.tick()                       # 超时 → ESCALATED
    case2.intervene(actor="销售经理", note="经理介入，亲自审核")
    case2.review("approve", actor="销售经理", note="经理终审通过")
    case2.send()
    show_case(case2, "异常分支：审核超时 → 自动升级经理 → 介入 → 终审通过")

    # ---------------- 异常分支：人工驳回打回 ----------------
    inquiry3 = next(e for e in emails if e.get("category") == "inquiry"
                    and e.get("id") not in (res["id"], res2["id"]))
    res3 = agent.process_one(inquiry3)
    case3 = WorkflowCase(res3)
    case3.run_pipeline()
    case3.review("reject", actor="业务员小王", note="数量单位待确认，打回重做")
    case3.resubmit(actor="业务员小王", note="补充单位后重新提交")
    case3.review("approve", actor="业务员小王", note="重做后通过")
    case3.send()
    show_case(case3, "异常分支：人工驳回 → 打回重做 → 重新提交 → 再审核通过")

    # 落盘
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump([c.summary() for c in (case, case2, case3)], f, ensure_ascii=False, indent=2)
    print(f"\n[已导出] {OUT}")


if __name__ == "__main__":
    main()
