# -*- coding: utf-8 -*-
"""
跨轮记忆演示（JD #2 记忆能力落地）
=================================

场景：Canada Decor Inc. 的两封续单邮件构成自然对话线程——
    E07  续单 PO-2026-0432，吸音吊顶板 600x600，1800 平，2026-11-20 前
    E17  又提 PO-2026-0432，但 2000 平、2026-12-15 前  ← 数量/交期变了

演示要证明的：处理完 E07 后，关于该客户合同的关键信息被存入跨轮记忆；
处理 E17（同 store）时，Agent 能召回 PO-2026-0432 的合同上下文，从而
(1) 不再重复追问已知信息，(2) 发现本次与上次的量/期差异并提示客户确认。

对照：同一封 E17 用「空白记忆」再跑一次，验证跨轮记忆带来的连续性差异。

零新增依赖。运行：python src/cross_turn_agent.py
作者：麦当 · 2026-09-04（Day06 续 · 跨轮记忆）
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memory_store import MemoryStore
from agent_loop import AgentLoop

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data", "e2e_emails.json")
MEM_PATH = os.path.join(BASE, "output", "memory_store.json")
CUSTOMER = "Canada Decor Inc."


def _load_customer(cust):
    with open(DATA, encoding="utf-8") as f:
        emails = json.load(f)
    rows = [e for e in emails
            if (e.get("expected", {}) or {}).get("customer") == cust or e.get("customer") == cust]
    # 按 id 排序，保证 E07 在 E17 前（续单顺序）
    rows.sort(key=lambda e: e.get("id", ""))
    return rows


def _brief(res, n=360):
    ans = (res.get("answer") or "").replace("\n", " ")
    return ans[:n] + ("..." if len(ans) > n else "")


def main():
    ap = argparse.ArgumentParser(description="跨轮记忆演示：Canada Decor E07→E17 续单线程")
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--no-compare", action="store_true", help="跳过「无记忆对照」以省成本")
    args = ap.parse_args()

    emails = _load_customer(CUSTOMER)
    if len(emails) < 2:
        print(f"数据不足：{CUSTOMER} 仅有 {len(emails)} 封邮件")
        return
    e07, e17 = emails[0], emails[1]

    # 共享记忆存储（文件备份，先清空保证演示干净）
    store = MemoryStore(MEM_PATH)
    store.reset()

    agent = AgentLoop(max_rounds=args.rounds, verbose=True)
    cust = CUSTOMER

    # ---- 第一封：E07，写入记忆 ----
    print("\n" + "=" * 64)
    print(f"① 处理 {e07.get('id')}（写入跨轮记忆）")
    print("=" * 64)
    r07 = agent.run(e07, memory_store=store)
    print(f"→ 模式 {r07.get('mode')} | finish {r07.get('finished')} | "
          f"¥{r07.get('cost_yuan'):.5f} | {r07.get('rounds')} 轮 | 工具 {r07.get('tool_calls')} 次")
    print("答案:", _brief(r07))

    # 优雅兜底：若模型未自主 remember，按 E07 处理结论预置合同记忆（明确标注，保证 E17 有上下文）
    if not store.get_customer(cust):
        store.remember(cust, "contract_spec",
                       "PO-2026-0432 = Acoustic ceiling panel 600x600（吸音吊顶板）；"
                       "联系人 Lisa；E07 续单 1800 平、2026-11-20 前、按前约合同价。",
                       mail_id=e07.get("id"))
        print("（模型未自主 remember，已按 E07 结论预置合同记忆以保证演示连贯）")
    else:
        print("（模型已自主写入记忆）")

    print("\n— 当前跨轮记忆 —")
    print(store.summary(cust))

    # ---- 第二封：E17，同一 store，召回记忆 ----
    print("\n" + "=" * 64)
    print(f"② 处理 {e17.get('id')}（同一 store，召回跨轮记忆）")
    print("=" * 64)
    r17 = agent.run(e17, memory_store=store)
    recalled = store.recall(customer=cust, query=e17.get("subject", ""))
    print(f"→ 模式 {r17.get('mode')} | finish {r17.get('finished')} | "
          f"¥{r17.get('cost_yuan'):.5f} | {r17.get('rounds')} 轮 | 工具 {r17.get('tool_calls')} 次")
    print("召回到的历史记忆:", [m["fact_type"] for m in recalled] or "（无）")
    print("答案:", _brief(r17))

    # ---- 对照：E17 用空白记忆再跑一次 ----
    if not args.no_compare:
        print("\n" + "=" * 64)
        print(f"③ 对照：{e17.get('id')} 用空白记忆（无跨轮上下文）")
        print("=" * 64)
        fresh = MemoryStore()
        fresh.reset()
        r17_no = agent.run(e17, memory_store=fresh)
        print(f"→ 模式 {r17_no.get('mode')} | finish {r17_no.get('finished')} | "
              f"¥{r17_no.get('cost_yuan'):.5f} | {r17_no.get('rounds')} 轮 | 工具 {r17_no.get('tool_calls')} 次")
        print("答案:", _brief(r17_no))

        print("\n— 跨轮收益对照 —")
        print(f"  带记忆  : ¥{r17.get('cost_yuan'):.5f} | 轮 {r17.get('rounds')} | 工具 {r17.get('tool_calls')} 次")
        print(f"  无记忆  : ¥{r17_no.get('cost_yuan'):.5f} | 轮 {r17_no.get('rounds')} | 工具 {r17_no.get('tool_calls')} 次")
        notediff = ("PO-2026-0432" in (r17.get("answer") or "")) and \
                   ("1,800" in (r17.get("answer") or "") or "1800" in (r17.get("answer") or ""))
        print(f"  连续性  : {'带记忆的答案引用了历史 PO/上次数量，体现跨轮连贯' if notediff else '（连续性需人工核对答案正文）'}")

    # 落盘结果
    out = {
        "customer": cust,
        "memories_after_e07": store.get_customer(cust),
        "e07": {"mode": r07.get("mode"), "finished": r07.get("finished"),
                "rounds": r07.get("rounds"), "cost_yuan": r07.get("cost_yuan")},
        "e17_with_memory": {"mode": r17.get("mode"), "finished": r17.get("finished"),
                            "rounds": r17.get("rounds"), "tool_calls": r17.get("tool_calls"),
                            "cost_yuan": r17.get("cost_yuan"),
                            "answer": r17.get("answer")},
    }
    if not args.no_compare:
        out["e17_without_memory"] = {"mode": r17_no.get("mode"), "finished": r17_no.get("finished"),
                                     "rounds": r17_no.get("rounds"), "tool_calls": r17_no.get("tool_calls"),
                                     "cost_yuan": r17_no.get("cost_yuan"),
                                     "answer": r17_no.get("answer")}
    with open(os.path.join(BASE, "output", "cross_turn_demo.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n✔ 结果已落盘 output/cross_turn_demo.json")


if __name__ == "__main__":
    main()
