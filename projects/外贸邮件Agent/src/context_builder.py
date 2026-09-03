# -*- coding: utf-8 -*-
"""
上下文组装 —— RAG 生成端（G）的第一步，也是忠实度评测的硬前置。

为什么必须先做这一步：
    忠实度评测的本质 = 「答案里的每句话，能不能在被引用的 chunk 里找到出处」。
    没有 [1][2] 引用编号，就不知道该去哪条 chunk 核对，评测直接无法进行。
    所以编号不是格式美化，是给「答案 → 来源 chunk」建立可审计的映射链。

处理顺序（不能乱）：
    1. 去重   —— 按 id 去重，再按 text 前缀去重（防同义重复占用预算）
    2. 截断   —— 整条粒度累加，要么整条要要么整条丢，绝不截断单条中间
    3. 编号   —— [1] [2] [3]，同时产出 id_map（编号 → 真实 chunk id）
    4. 溯源   —— map_citations() 把答案里的编号映射回 chunk id

零依赖，纯标准库。
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from retriever import BM25Retriever, load_corpus, build_default, CORPUS_PATH  # noqa: E402

try:
    from vector_retriever import build_hybrid
except Exception:  # 智谱 key 缺失或依赖异常时，允许只用 BM25
    build_hybrid = None


# ----------------------------------------------------------------------
# 可调常量
# ----------------------------------------------------------------------
DEFAULT_MAX_CHARS = 2000      # 上下文预算（字符）。语料 21 条约 2100 字符，默认不会触发截断
DEDUP_PREFIX = 60             # 按 text 前 N 字判重
EMPTY_CONTEXT = "（无相关历史记录）"
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
CITE_RE = re.compile(r"\[(\d+)\]")


# ----------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------
def est_tokens(s):
    """粗估 token 数：中日韩字符按 1 token/字，其余按 4 字符 1 token。

    只用于成本/预算观察，不用于计费。
    """
    if not s:
        return 0
    cjk = len(CJK_RE.findall(s))
    other = len(s) - cjk
    return int(cjk + other / 4.0)


def render_line(idx, hit):
    """渲染一行上下文：[编号] (chunk_id | 客户名) 正文"""
    cid = hit.get("id", "?")
    cust = hit.get("customer") or "未知客户"
    text = (hit.get("text") or "").strip()
    return f"[{idx}] ({cid} | {cust}) {text}"


def map_citations(cited_nums, id_map):
    """把答案里出现的编号 [1][2] 映射回真实 chunk id。

    无效编号（如模型编造的 [7]）会被单独返回，用于「引用准确率」评测。
    返回 (valid_ids, invalid_nums)
    """
    valid, invalid = [], []
    for n in cited_nums:
        key = str(n)
        if key in id_map:
            if id_map[key] not in valid:
                valid.append(id_map[key])
        else:
            invalid.append(n)
    return valid, invalid


def extract_citations(answer):
    """从答案文本里抽取所有引用编号（按出现顺序去重）。"""
    seen, out = set(), []
    for m in CITE_RE.finditer(answer or ""):
        n = m.group(1)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ----------------------------------------------------------------------
# 核心
# ----------------------------------------------------------------------
def build_context(query, hits, max_chars=DEFAULT_MAX_CHARS):
    """把检索命中组装成带编号的上下文。

    参数
        query     : 原始查询（仅用于回传，方便日志追溯）
        hits      : retriever.search() 的返回，list[dict]，含 id/customer/text/score
        max_chars : 上下文字符预算，整条粒度截断

    返回 dict：
        context         组装好的上下文字符串（无命中时为 EMPTY_CONTEXT）
        id_map          {"1": "C07", ...}  编号 → chunk id
        kept            实际保留的 hits（顺序即编号顺序）
        n_input         输入条数
        n_dup_dropped   去重丢掉条数
        n_trunc_dropped 截断丢掉条数
        truncated       是否触发过截断
        chars / est_tokens / max_chars
    """
    hits = list(hits or [])

    # —— 1. 去重：先按 id，再按 text 前缀 ——
    seen_ids, seen_prefix, uniq = set(), set(), []
    for h in hits:
        cid = h.get("id")
        prefix = (h.get("text") or "")[:DEDUP_PREFIX].strip()
        if cid and cid in seen_ids:
            continue
        if prefix and prefix in seen_prefix:
            continue
        if cid:
            seen_ids.add(cid)
        if prefix:
            seen_prefix.add(prefix)
        uniq.append(h)
    n_dup_dropped = len(hits) - len(uniq)

    # —— 2. 截断：整条粒度累加，绝不截断单条中间 ——
    kept, used, truncated = [], 0, False
    for h in uniq:
        # 预留编号前缀长度，避免渲染后超预算
        line = render_line(len(kept) + 1, h)
        if used + len(line) > max_chars and kept:
            # 至少保留 1 条；预算装不下就从这里开始丢
            truncated = True
            break
        kept.append(h)
        used += len(line) + 1  # +1 换行
    n_trunc_dropped = len(uniq) - len(kept)

    # —— 3. 编号 + id_map ——
    lines, id_map = [], {}
    for i, h in enumerate(kept, start=1):
        lines.append(render_line(i, h))
        id_map[str(i)] = h.get("id")

    context = "\n".join(lines) if lines else EMPTY_CONTEXT

    return {
        "query": query,
        "context": context,
        "id_map": id_map,
        "kept": kept,
        "n_input": len(hits),
        "n_dup_dropped": n_dup_dropped,
        "n_trunc_dropped": n_trunc_dropped,
        "truncated": truncated,
        "chars": len(context),
        "est_tokens": est_tokens(context),
        "max_chars": max_chars,
    }


def build_from_query(query, retriever, top_k=5, max_chars=DEFAULT_MAX_CHARS):
    """一步到位：检索 → 组装。便于评测脚本直接调用。"""
    hits = retriever.search(query, top_k=top_k, return_scores=True)
    return build_context(query, hits, max_chars=max_chars)


# ----------------------------------------------------------------------
# Demo
# ----------------------------------------------------------------------
DEMO_QUERIES = [
    "哪些客户问过 LED Downlight？",
    "有没有客户投诉过质量问题？",
    "Global Import 的历史报价是多少？",
]


def _get_retriever(prefer_hybrid=True):
    """优先混合检索，失败降级 BM25（沿用项目一贯的优雅降级风格）。"""
    if prefer_hybrid and build_hybrid is not None:
        try:
            r, corpus = build_hybrid()
            print(f"[检索器] HybridRetriever（BM25 + {r.emb_name}）| 语料 {len(corpus)} 条")
            return r
        except Exception as e:
            print(f"[警告] 混合检索不可用（{type(e).__name__}: {e}），降级 BM25")
    r, corpus = build_default()
    print(f"[检索器] BM25Retriever（降级）| 语料 {len(corpus)} 条")
    return r


def _show(query, bundle, label):
    print(f"\n{'=' * 68}")
    print(f"{label}")
    print(f"Query: {query}")
    print("-" * 68)
    print(bundle["context"])
    print("-" * 68)
    print(
        f"输入 {bundle['n_input']} 条 | 去重丢 {bundle['n_dup_dropped']} | "
        f"截断丢 {bundle['n_trunc_dropped']} | 触发截断: {'是' if bundle['truncated'] else '否'}"
    )
    print(f"上下文 {bundle['chars']} 字符 / 约 {bundle['est_tokens']} token（预算 {bundle['max_chars']}）")
    print(f"id_map: {bundle['id_map']}")


def main():
    ap = argparse.ArgumentParser(description="上下文组装：去重 / 截断 / 引用编号")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    ap.add_argument("--bm25", action="store_true", help="只用 BM25（不调智谱 embedding）")
    ap.add_argument("--query", type=str, default=None, help="只跑这一条 query")
    args = ap.parse_args()

    retr = _get_retriever(prefer_hybrid=not args.bm25)
    queries = [args.query] if args.query else DEMO_QUERIES

    # ① 正常预算
    for q in queries:
        b = build_from_query(q, retr, top_k=args.top_k, max_chars=args.max_chars)
        _show(q, b, f"【正常预算 max_chars={args.max_chars}】")

    # ② 强制小预算 —— 证明截断逻辑真的生效（写了但没验证 = 等于没写）
    if not args.query:
        q = queries[0]
        b = build_from_query(q, retr, top_k=args.top_k, max_chars=200)
        _show(q, b, "【强制小预算 max_chars=200 —— 验证截断生效】")

        # ③ 溯源自检：模拟一个带引用的答案，验证 id_map 能映射回去
        print(f"\n{'=' * 68}")
        print("【溯源自检】模拟答案里的引用 → 映射回真实 chunk id")
        print("-" * 68)
        b = build_from_query(q, retr, top_k=args.top_k, max_chars=args.max_chars)
        fake_answer = "Global Import Ltd. 曾询盘 LED Panel Light [1]，另有客户问过同类产品 [2][9]。"
        print(f"模拟答案: {fake_answer}")
        nums = extract_citations(fake_answer)
        valid, invalid = map_citations(nums, b["id_map"])
        print(f"抽到编号: {nums}")
        print(f"有效 → chunk id: {valid}")
        print(f"无效（模型编造）: {invalid}  ← 这就是「引用准确率」要抓的")
    return 0


if __name__ == "__main__":
    sys.exit(main())
