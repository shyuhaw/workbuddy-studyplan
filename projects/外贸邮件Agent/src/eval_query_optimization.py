# -*- coding: utf-8 -*-
"""
检索 query 构造方式对比实验（Day06 H1 假设验证）
================================================
**这个实验只需要智谱 embedding + 本地 BM25，不消耗 DeepSeek 额度。**

背景（Day06 A/B 的关键归因）
---------------------------
Agent 可用率 45.5% vs 流水线 18.2%，差距的**真正来源不是模型更聪明**，
是 Agent 自己构造了更好的检索 query：

    流水线：`Bright Future Ltd`
    Agent  ：`Bright Future Ltd LED panel light 600x600 quotation price FOB Ningbo`

H1 假设：把流水线的 query 构造改好，可用率应能提升到接近 Agent 水平，**而成本几乎不变**。

为什么用代理指标而不是 recall@K
-------------------------------
邮件场景没有「这封邮件该命中哪些 chunk」的 ground truth 标注，
所以不算 recall@K，改测三个**直接决定答案能不能用**的代理指标：

    1. bm25_hit_rate      top-k 中存在 BM25 score>0 的比例（有关键词重叠 = 检索器认为相关）
    2. vec_top1_cosine    向量侧最高余弦相似度（语义相关性，越接近 1 越相关）
    3. non_empty_ctx_rate build_context 后上下文非空的比例 ← **这个直接决定答案可不可用**

第 3 个是最关键的一环：上下文为空 → LLM 只能答「依据现有记录无法确认」→ 答案不可用。
Day06 的 11 条样本里 9 条栽在这里。

对比三种 query 构造
-------------------
    A_current  f"{customer} {product}"                        ← 现状（workflow.py 用的）
    B_subject  f"{customer} {product} {subject}"              ← 加邮件主题
    C_rich     f"{customer} {product} {subject} {意图词}"      ← 再加意图/规格词（模拟 Agent）

作者：麦当
日期：2026-09-04（Day06）
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vector_retriever import build_hybrid  # noqa: E402
from extractor import extract_fields  # noqa: E402
from context_builder import build_context, EMPTY_CONTEXT  # noqa: E402

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(BASE_DIR, "data", "e2e_emails.json")
OUT_PATH = os.path.join(BASE_DIR, "output", "eval_query_optimization.json")

TOP_K = 3

# 意图/规格词：外贸询盘里真正区分「要查什么」的信号词
INTENT_TERMS = [
    "quotation", "price", "cost", "unit price", "specification", "model",
    "delivery", "lead time", "shipment", "payment", "order", "quantity",
    "报价", "价格", "规格", "型号", "交期", "付款", "订单", "数量",
]


def build_query_current(mail, fields):
    """A：现状 —— workflow.py::_step_retrieve 用的就是它"""
    cust = fields.get("customer", {}).get("value") or ""
    prod = fields.get("product", {}).get("value") or ""
    return f"{cust} {prod}".strip() or mail.get("subject", "")


def build_query_subject(mail, fields):
    """B：客户 + 产品 + 邮件主题"""
    cust = fields.get("customer", {}).get("value") or ""
    prod = fields.get("product", {}).get("value") or ""
    return f"{cust} {prod} {mail.get('subject', '')}".strip()


def build_query_rich(mail, fields):
    """C：客户 + 产品 + 主题 + 从正文抽到的意图/规格词（模拟 Agent 的 query 构造）"""
    base = build_query_subject(mail, fields)
    text = f"{mail.get('subject', '')} {mail.get('body', '')}".lower()
    hit_terms = [t for t in INTENT_TERMS if t in text]
    # 最多补 3 个，避免 query 被无关词稀释
    return f"{base} {' '.join(hit_terms[:3])}".strip()


BUILDERS = {
    "A_current": build_query_current,
    "B_subject": build_query_subject,
    "C_rich": build_query_rich,
}


def measure(retriever, mail, fields, builder):
    """跑一次检索，返回三项代理指标"""
    query = builder(mail, fields)
    hits = retriever.search(query, top_k=TOP_K)

    bm25_hit = any(h.get("score", 0) > 0 for h in hits)
    # 向量侧余弦：HybridRetriever 内部算过，这里用 rerank/score 无法直接拿，
    # 退而用「检索分>0 的条数」+「上下文是否非空」作为可用性代理
    bundle = build_context(query, hits)
    non_empty = bundle["context"] != EMPTY_CONTEXT and bool(bundle["id_map"])

    return {
        "query": query,
        "bm25_hit": bm25_hit,
        "non_empty_ctx": non_empty,
        "n_kept": len(bundle["kept"]),
        "top_ids": [h.get("id") for h in hits[:TOP_K]],
        "chars": bundle["chars"],
    }


def main():
    ap = argparse.ArgumentParser(description="检索 query 构造方式对比实验")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 封（默认全跑 20 封）")
    args = ap.parse_args()

    with open(DATA_PATH, encoding="utf-8") as f:
        emails = json.load(f)
    if args.limit:
        emails = emails[:args.limit]

    print(f"样本：{len(emails)} 封真实邮件 | 对比 {len(BUILDERS)} 种 query 构造\n")
    print("建库（智谱 embedding + 本地 BM25）...")
    retriever, corpus = build_hybrid()
    print(f"语料 {len(corpus)} 条\n")

    results = []
    for i, mail in enumerate(emails, 1):
        fields = extract_fields(mail)
        row = {"mail_id": mail.get("id"), "subject": mail.get("subject", "")[:50]}
        for name, builder in BUILDERS.items():
            row[name] = measure(retriever, mail, fields, builder)
        results.append(row)
        a, b, c = row["A_current"], row["B_subject"], row["C_rich"]
        print(f"[{i:>2}/{len(emails)}] {mail.get('id')} "
              f"A:{'✅' if a['non_empty_ctx'] else '❌'} "
              f"B:{'✅' if b['non_empty_ctx'] else '❌'} "
              f"C:{'✅' if c['non_empty_ctx'] else '❌'}  "
              f"{mail.get('subject', '')[:38]}")

    # ---------------- 汇总 ----------------
    n = len(results)

    def rate(key, grp, sub):
        return round(sum(1 for r in results if r[grp][sub]) / n, 3)

    def avg(key_none, grp, sub):
        vals = [r[grp][sub] for r in results]
        return round(sum(vals) / len(vals), 2)

    summary = {"n_samples": n, "top_k": TOP_K,
               "ts": __import__("time").strftime("%Y-%m-%d %H:%M:%S")}
    for g in BUILDERS:
        summary[g] = {
            "bm25_hit_rate": rate(g, g, "bm25_hit"),
            "non_empty_ctx_rate": rate(g, g, "non_empty_ctx"),
            "avg_kept": avg(None, g, "n_kept"),
            "avg_chars": avg(None, g, "chars"),
        }

    # 逐条：A 失败但 B/C 成功的样本 = query 优化的直接收益
    gained = []
    for r in results:
        if not r["A_current"]["non_empty_ctx"]:
            best = None
            for g in ("C_rich", "B_subject"):
                if r[g]["non_empty_ctx"]:
                    best = g
                    break
            if best:
                gained.append({
                    "mail_id": r["mail_id"],
                    "subject": r["subject"],
                    "A_query": r["A_current"]["query"],
                    "fixed_by": best,
                    "fixed_query": r[best]["query"],
                    "top_ids": r[best]["top_ids"],
                })
    summary["gained_by_optimization"] = gained

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "results": results}, f, ensure_ascii=False, indent=2)

    # ---------------- 打印 ----------------
    print("\n" + "=" * 70)
    print(f"检索 query 构造对比（{n} 封真实邮件 · top_k={TOP_K}）")
    print("=" * 70)
    print(f"{'指标':<26}{'A 现状':>14}{'B +主题':>14}{'C +意图词':>14}")
    print("-" * 70)
    for label, key in [("BM25 命中率", "bm25_hit_rate"),
                       ("**上下文非空率**", "non_empty_ctx_rate"),
                       ("平均保留条数", "avg_kept"),
                       ("平均上下文字符", "avg_chars")]:
        print(f"{label:<26}"
              f"{summary['A_current'][key]:>14.3f}"
              f"{summary['B_subject'][key]:>14.3f}"
              f"{summary['C_rich'][key]:>14.3f}")

    print("-" * 70)
    print(f"\n【A 检索为空、但优化后有结果的样本】{len(gained)}/{n}")
    for g in gained:
        print(f"  · {g['mail_id']} {g['subject'][:36]}")
        print(f"    A: \"{g['A_query'][:60]}\"")
        print(f"    {g['fixed_by']}: \"{g['fixed_query'][:60]}\" → 命中 {g['top_ids']}")

    a_rate = summary["A_current"]["non_empty_ctx_rate"]
    c_rate = summary["C_rich"]["non_empty_ctx_rate"]
    print("\n" + "=" * 70)
    if c_rate > a_rate:
        lift = (c_rate - a_rate) / a_rate * 100 if a_rate else 0
        print(f"✅ H1 成立：上下文非空率 {a_rate:.1%} → {c_rate:.1%}（+{lift:.0f}%）")
        print("   → 只需改一行 query 构造，就能拿到 Agent 的大部分收益，成本几乎不变")
    else:
        print(f"❌ H1 不成立：优化后非空率未提升（{a_rate:.1%} → {c_rate:.1%}）")
        print("   → 说明可用率差距另有原因，需要重新归因")
    print("=" * 70)
    print(f"\n落盘：{OUT_PATH}")


if __name__ == "__main__":
    main()
