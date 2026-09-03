# -*- coding: utf-8 -*-
"""
BM25 检索器（纯 Python，零依赖）
=================================
为询盘响应工作流提供「客户历史档案」检索能力。

为什么自己写而不装 rank_bm25：
    环境无网络/无该包，且 Okapi BM25 算法本身很薄（约 30 行）。
    自己实现 = 可离线、可复现、可解释，面试被追问 k1/b 含义时能把公式背出来，
    比 `from rank_bm25 import BM25Okapi` 一行调用更有说服力。
    与 rank_bm25.BM25Okapi 数学等价。

检索链路（RAG 五环节之「检索」）：
    语料(自然语言陈述句) → 分词 → 建倒排 → query 分词 → BM25 打分 → top_k

分词策略：
    - 英文/数字：按 [a-z0-9]+ 提取并转小写（产品型号 PL-6060、价格 12.80、数量 3000 都是关键信号，必须保留）
    - 中文：按字切分（轻量兜底，让中文短词能部分命中；已知局限：无法做语义匹配，
      这正是 Day03「明天加向量检索」要补的——本 baseline 故意只做关键词召回）

作者：麦当
日期：2026-09-01
"""

import os
import re
import json
import math

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS_PATH = os.path.join(BASE_DIR, "data", "customer_corpus.json")

# Okapi BM25 超参：k1 控制词频饱和度，b 控制文档长度归一化
K1 = 1.5
B = 0.75

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text):
    """分词：英文数字小写为词，中文按字切。"""
    text = (text or "").lower()
    tokens = list(_TOKEN_RE.findall(text))
    # 中文按字补切，提升中文短词部分召回
    for ch in text:
        if "一" <= ch <= "鿿":
            tokens.append(ch)
    return tokens


class BM25Retriever:
    def __init__(self, k1=K1, b=B):
        self.k1 = k1
        self.b = b
        self.docs = []        # 原始 chunk 列表
        self.tok_docs = []    # 分词后的文档
        self.doc_lens = []
        self.avgdl = 0.0
        self.df = {}
        self.N = 0
        self._built = False

    # ------------------------------------------------------------------
    def build(self, chunks):
        """chunks: list[dict]，每个含 id/text（其余字段原样保留）"""
        self.docs = chunks
        self.tok_docs = [tokenize(c.get("text", "")) for c in chunks]
        self.doc_lens = [len(t) for t in self.tok_docs]
        self.N = len(self.tok_docs)
        self.avgdl = (sum(self.doc_lens) / self.N) if self.N else 0.0

        df = {}
        for toks in self.tok_docs:
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        self.df = df
        self._built = True
        return self

    def _idf(self, t):
        # BM25 标准 idf（加 1 平滑避免负无穷）
        return math.log(1 + (self.N - self.df.get(t, 0) + 0.5) /
                        (self.df.get(t, 0) + 0.5))

    def _score(self, q_toks, d_idx):
        """单文档 BM25 打分（仅对 query 中的唯一词累加）"""
        f = {}
        for t in self.tok_docs[d_idx]:
            f[t] = f.get(t, 0) + 1
        dl = self.doc_lens[d_idx]
        score = 0.0
        for t in set(q_toks):
            if t not in self.df:
                continue
            ft = f.get(t, 0)
            if ft == 0:
                continue
            idf = self._idf(t)
            score += idf * (ft * (self.k1 + 1)) / (
                ft + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        return score

    def search(self, query, top_k=3, return_scores=True):
        """返回 top_k 个命中 chunk（含得分），按得分降序。"""
        if not self._built:
            raise RuntimeError("请先调用 build() 建库")
        q_toks = tokenize(query)
        if not q_toks:
            return []
        scored = [(self._score(q_toks, i), i) for i in range(self.N)]
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for s, i in scored[:top_k]:
            hit = dict(self.docs[i])
            if return_scores:
                hit["score"] = round(s, 4)
            out.append(hit)
        return out

    def explain(self, query, top_k=3):
        """返回可读的检索解释（用于作品集/调试展示）"""
        q_toks = tokenize(query)
        hits = self.search(query, top_k=top_k)
        return {
            "query_tokens": q_toks,
            "hits": [{"id": h["id"], "score": h.get("score"),
                      "customer": h.get("customer"),
                      "text": h.get("text")} for h in hits],
        }


def load_corpus(path=CORPUS_PATH):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["chunks"]


def build_default(path=CORPUS_PATH):
    """一键加载语料并建库，返回 (retriever, corpus)"""
    chunks = load_corpus(path)
    return BM25Retriever().build(chunks), chunks


if __name__ == "__main__":
    retr, corpus = build_default()
    print(f"语料规模: {len(corpus)} 条 chunk")
    for q in [
        "哪些客户问过 LED Downlight？",
        "报过 USD 12.80 的是哪家？",
        "有没有客户投诉过质量问题？",
    ]:
        print(f"\nQuery: {q}")
        for h in retr.search(q, top_k=2):
            print(f"  [{h['score']:.3f}] {h['id']} {h['customer']}: {h['text'][:60]}...")
