# -*- coding: utf-8 -*-
"""
RAG 检索评测（BM25 baseline / 混合检索 hybrid）
================================================
钉死 baseline 数字，对比「加向量检索」后的效果提升。

指标：recall@k —— 一条 query 只要在 top-k 里命中至少一个预期 chunk 即算召回成功。
     整体 recall@k = 召回成功的 query 数 / 总 query 数。

分两类汇报：
    ① 全部 20 条（展示整体水准）
    ② 仅 semantic 类（预先判断 BM25 会失败，诚实展示关键词检索的召回天花板）

用法：
    python src/eval_retrieval.py                # BM25 baseline
    python src/eval_retrieval.py --mode hybrid  # BM25 + 智谱向量(RRF 融合)
输出：recall@1/@3/@5 + 逐条明细 + 落盘 output/eval_retrieval.json

作者：麦当
日期：2026-09-01
"""

import os
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from retriever import build_default, load_corpus
from vector_retriever import build_hybrid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_PATH = os.path.join(BASE_DIR, "data", "eval_queries.json")
OUT_PATH = os.path.join(BASE_DIR, "output", "eval_retrieval.json")


def recall_at_k(retrieved_ids_per_query, expects_per_query, k):
    """retrieved_ids_per_query: list[list[str]]（已按得分排序）
       expects_per_query:        list[list[str]]
       返回 recall@k（0~1）"""
    hit = 0
    for retrieved, expects in zip(retrieved_ids_per_query, expects_per_query):
        if set(retrieved[:k]) & set(expects):
            hit += 1
    return hit / len(expects_per_query) if expects_per_query else 0.0


def mrr_at_k(retrieved_ids_per_query, expects_per_query, k):
    """Mean Reciprocal Rank @k：每个 query 取最高相关 chunk 的排名倒数均值。
    若 top-k 内无相关 chunk，则该 query 贡献 0。"""
    rr_sum = 0.0
    count = len(expects_per_query)
    for retrieved, expects in zip(retrieved_ids_per_query, expects_per_query):
        top_k = retrieved[:k]
        for rank, rid in enumerate(top_k, start=1):
            if rid in expects:
                rr_sum += 1.0 / rank
                break
    return rr_sum / count if count else 0.0


def main(mode="bm25", enable_rewrite=True, w_vec=1.0, w_bm25=1.0,
         auto_weight=True):
    retr, _ = (build_hybrid(enable_rewrite=enable_rewrite, w_vec=w_vec, w_bm25=w_bm25,
                            auto_weight=auto_weight)
               if mode == "hybrid" else build_default())
    with open(EVAL_PATH, "r", encoding="utf-8") as f:
        ev = json.load(f)
    queries = ev["queries"]

    rows = []
    retrieved_all, expects_all = [], []
    retrieved_sem, expects_sem = [], []
    for q in queries:
        hits = retr.search(q["query"], top_k=5, return_scores=True)
        rids = [h["id"] for h in hits]
        exp = q["expects"]
        retrieved_all.append(rids)
        expects_all.append(exp)
        if q.get("expected_fail"):
            retrieved_sem.append(rids)
            expects_sem.append(exp)
        ok = bool(set(rids[:3]) & set(exp))
        rows.append({
            "id": q["id"], "type": q["type"], "query": q["query"],
            "expects": exp, "top3": rids[:3],
            "hit@3": ok, "expected_fail": q.get("expected_fail", False),
            "scores": [h.get("score") for h in hits[:3]],
        })

    ks = [1, 3, 5]
    overall = {f"recall@{k}": round(recall_at_k(retrieved_all, expects_all, k), 3) for k in ks}
    semantic = {f"recall@{k}": round(recall_at_k(retrieved_sem, expects_sem, k), 3) for k in ks}
    # 新增 MRR 指标
    overall_mrr = {f"mrr@{k}": round(mrr_at_k(retrieved_all, expects_all, k), 3) for k in ks}
    semantic_mrr = {f"mrr@{k}": round(mrr_at_k(retrieved_sem, expects_sem, k), 3) for k in ks}

    title = ("RAG 检索评测 · 混合检索（BM25+向量 RRF）"
             if mode == "hybrid" else "RAG 检索评测 · BM25 baseline")
    if mode == "hybrid" and not enable_rewrite:
        title += "（已关闭 query rewriting，对照用）"
    if mode == "hybrid" and (w_vec != 1.0 or w_bm25 != 1.0):
        title += f"（加权 w_vec={w_vec} w_bm25={w_bm25}）"
    if mode == "hybrid" and auto_weight:
        title += "（意图自适应调权：数字类 BM25 主导 / 语义类向量主导）"
    print("=" * 82)
    print(title)
    print("=" * 82)
    print(f"语料 chunk 数: {len(load_corpus())}  | 评测 query 数: {len(queries)}")
    print("\n【整体指标】")
    for k in ks:
        print(f"  recall@{k:<2} = {overall[f'recall@{k}']:.1%} | mrr@{k:<2} = {overall_mrr[f'mrr@{k}']:.3f}")
    print("\n【仅语义类（BM25 预计失败，诚实展示天花板）】")
    for k in ks:
        print(f"  recall@{k:<2} = {semantic[f'recall@{k}']:.1%} | mrr@{k:<2} = {semantic_mrr[f'mrr@{k}']:.3f}  "
              f"({sum(1 for q in queries if q.get('expected_fail'))} 条)")
    print("\n【逐条明细】")
    for r in rows:
        tag = "✗预计失败" if r["expected_fail"] else " "
        print(f"  {r['id']} [{r['type']:<12}] {'✓' if r['hit@3'] else '✗'} {tag} "
              f"{r['query']}")
        if not r["hit@3"]:
            print(f"       预期 {r['expects']} ｜ top3 {r['top3']} ｜ scores {r['scores']}")

    result = {"mode": mode, "enable_rewrite": enable_rewrite,
              "auto_weight": auto_weight, "w_vec": w_vec, "w_bm25": w_bm25,
              "overall": overall, "semantic_only": semantic,
              "overall_mrr": overall_mrr, "semantic_mrr": semantic_mrr,
              "details": rows, "corpus_size": len(load_corpus()),
              "query_count": len(queries)}
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n[已导出] {OUT_PATH}")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["bm25", "hybrid"], default="bm25")
    ap.add_argument("--no-rewrite", action="store_true",
                    help="关闭 query rewriting（仅 hybrid 有意义，用于对照）")
    ap.add_argument("--w-vec", type=float, default=3.0, help="向量融合权重（语义类）")
    ap.add_argument("--w-bm25", type=float, default=1.0, help="BM25 融合权重")
    ap.add_argument("--no-auto", action="store_true",
                    help="关闭意图自适应调权（数字类也用统一权重，用于对照）")
    args = ap.parse_args()
    main(args.mode, enable_rewrite=not args.no_rewrite,
         w_vec=args.w_vec, w_bm25=args.w_bm25, auto_weight=not args.no_auto)
