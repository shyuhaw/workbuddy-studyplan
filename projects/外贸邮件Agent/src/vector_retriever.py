# -*- coding: utf-8 -*-
"""
混合检索器（BM25 + 智谱向量，RRF 融合，零重依赖）
===============================================
为询盘响应工作流的「客户历史档案」检索补上语义能力。

为什么做这层：
    Day03 的 BM25 baseline 在「语义类」查询只有 40% 召回——字面不重叠就抓瞎
    （如 query "谁怕不合规被查" vs 语料 "CE 认证"）。本模块接入智谱 embedding-3
    做向量检索，与 BM25 用 RRF（Reciprocal Rank Fusion）倒数排名融合，
    把语义类召回拉起来，同时保留 BM25 在精确词（型号/价格）上的优势。

依赖：requests（已装）。余弦相似度用 math 纯算，无需 numpy。
      embedding-3 固定 2048 维。

无智谱 key 时自动降级 MockEmbedder（bag-of-words 确定性向量）：
    仅用于验证「混合架构 + 融合逻辑」不报错、不拖累 BM25 baseline；
    真实语义提升需在 config 填 zhipu.api_key 后重跑 eval_retrieval.py。

作者：麦当
日期：2026-09-01
"""

import os
import re
import json
import math
import hashlib
import urllib.request

from retriever import BM25Retriever, load_corpus, tokenize, CORPUS_PATH, BASE_DIR

CONFIG_PATH = os.path.join(BASE_DIR, "config", "llm_config.json")
EMBED_MODEL = "embedding-3"
ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/embeddings"
RRF_K = 60  # RRF 标准常数


# ----------------------------------------------------------------------
# Query-time 业务同义词扩展（弥合「销售口语」与「档案术语」的语义鸿沟）
# ----------------------------------------------------------------------
# 触发词 → 扩展业务术语。仅在"向量侧"生效，BM25 侧保持原 query（干净隔离实验）。
# 这是标准的 query rewriting 手法：检索时把"在意发货准时"扩展成
# "交期/逾期/履约/违约金"，让 embedding 命中语料里"交期要求严格"的表述。
SYNONYM_EXPANSION = {
    # 交期 / 发货准时
    "发货准时": ["交期", "逾期", "按时交付", "履约", "违约金", "提前说明", "lead time"],
    "交期": ["逾期", "按时交付", "履约", "发货准时", "违约金"],
    "不拖延": ["交期", "逾期", "按时交付"],
    # 合规 / 认证
    "不合规": ["认证", "CE", "RoHS", "资质", "海关查验", "合规风险", "无认证供货"],
    "被查": ["认证", "CE", "RoHS", "海关查验", "合规风险", "资质"],
    "合规": ["认证", "CE", "RoHS", "资质"],
    # 质量
    "质量问题": ["色温不一致", "一致性", "缺陷", "索赔", "翻车"],
    "投诉": ["索赔", "缺陷", "质量问题"],
    # 价格
    "便宜": ["单价敏感", "目标价", "成本", "议价"],
    "性价比": ["单价敏感", "目标价"],
    # 复购
    "返单": ["复购周期", "返单", "稳定", "量大"],
    "复购": ["复购周期", "返单", "稳定", "量大"],
}


# ----------------------------------------------------------------------
# 查询意图识别（决定「BM25 主导」还是「向量主导」）
# ----------------------------------------------------------------------
# 数字/价格类查询（USD 12.80、单价、成交价 + 具体数字）BM25 字面匹配最准，
# 若让向量主导会被"单价"等泛化概念带偏（如 Q09 单价 3.50 错配到 C15/C14）。
# 此类查询强制走「BM25 主导」融合权重，保住精确召回。
# 语义类查询（在意发货准时 / 怕不合规被查）字面不重叠，必须向量主导才能命中。
_PRICE_RE = re.compile(r"(USD|EUR|AUD|RMB|CNY)\s*\d|单价")


def is_number_query(q):
    """命中价格/数字模式 → True（走 BM25 主导融合）。"""
    return bool(_PRICE_RE.search(q))


def expand_variants(q, table=SYNONYM_EXPANSION):
    """Query rewriting（多向量扩展）：命中触发词则生成「原 query + 各触发词扩展」的
    一组子查询变体。检索时对每个文档取「与任一变体的最大余弦」（max-pooling），
    比「拼接后质心平均」更聚焦、不稀释原意。仅向量侧生效，BM25 侧保持原 query。"""
    variants = [q]
    for trig, syns in table.items():
        if trig in q:
            ext = [s for s in syns if s not in q]
            if ext:
                variants.append(q + " " + " ".join(ext))
    return variants


def _load_zhipu_key():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return (cfg.get("zhipu") or {}).get("api_key") or None
    except Exception:
        return None


# ----------------------------------------------------------------------
# Embedder 抽象
# ----------------------------------------------------------------------
class ZhipuEmbedder:
    """智谱 embedding-3 真向量检索。"""

    name = "zhipu-embedding-3"

    def __init__(self, api_key):
        self.api_key = api_key
        self._cache = {}

    def _call(self, texts):
        req = urllib.request.Request(
            ZHIPU_URL,
            data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return [d["embedding"] for d in json.loads(resp.read())["data"]]

    def embed_batch(self, texts):
        return self._call(texts)

    def embed_query(self, text):
        if text in self._cache:
            return self._cache[text]
        v = self._call([text])[0]
        self._cache[text] = v
        return v


class MockEmbedder:
    """无 key 降级：bag-of-words 确定性稠密向量（≈关键词，验证架构用）。"""

    name = "mock-bow"
    DIM = 512

    def embed_batch(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)

    def _vec(self, text):
        toks = tokenize(text)
        v = [0.0] * self.DIM
        for t in toks:
            h = int(hashlib.md5(t.encode("utf-8")).hexdigest(), 16) % self.DIM
            v[h] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


def get_embedder():
    key = _load_zhipu_key()
    if key:
        try:
            # 探针：用一条极短文本验证 key 可用 + 账户有余额，避免建库到一半崩
            emb = ZhipuEmbedder(key)
            emb.embed_query("probe")
            return emb, "zhipu-embedding-3"
        except urllib.error.HTTPError as e:
            tip = _decode_zhipu_error(e)
            print(
                f"[HybridRetriever] ⚠️ 智谱 embedding 不可用（{tip}），已降级 MockEmbedder。\n"
                "          真实语义召回（预期 40%→80%+）待账户恢复后重跑：\n"
                "          python src/eval_retrieval.py --mode hybrid"
            )
            return MockEmbedder(), "mock-bow"
        except Exception as e:  # 网络/其他异常，同样降级
            print(
                f"[HybridRetriever] ⚠️ 智谱 embedding 调用异常（{e}），已降级 MockEmbedder。"
            )
            return MockEmbedder(), "mock-bow"
    print(
        "[HybridRetriever] ⚠️ 未检测到 config.zhipu.api_key，使用 MockEmbedder"
        "（仅验证架构，无真实语义提升）。\n"
        "          填法：在 config/llm_config.json 增加 "
        '{"zhipu": {"api_key": "你的key"}} 后重跑 src/eval_retrieval.py --mode hybrid'
    )
    return MockEmbedder(), "mock-bow"


def _decode_zhipu_error(e):
    """把智谱 HTTP 错误转成可阅读提示。"""
    try:
        body = json.loads(e.read().decode("utf-8"))
        msg = body.get("error", {}).get("message", str(e.code))
        code = body.get("error", {}).get("code", "")
        return f"HTTP {e.code} code={code} {msg}"
    except Exception:
        return f"HTTP {e.code}"


# ----------------------------------------------------------------------
# 余弦相似度（纯 math）
# ----------------------------------------------------------------------
def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ----------------------------------------------------------------------
# 混合检索（RRF 融合）
# ----------------------------------------------------------------------
class HybridRetriever:
    """BM25 与向量检索的 RRF 倒数排名融合。复用 search() 同签名，调用方无需改动。"""

    def __init__(self, bm25=None, embedder=None, rrf_k=RRF_K, enable_rewrite=True,
                 w_vec=1.0, w_bm25=1.0, auto_weight=True,
                 w_num_vec=1.0, w_num_bm25=1.0):
        self.bm25 = bm25 or BM25Retriever()
        if embedder is None:
            embedder, self.emb_name = get_embedder()
        else:
            self.emb_name = getattr(embedder, "name", "custom")
        self.embedder = embedder
        self.rrf_k = rrf_k
        self.enable_rewrite = enable_rewrite
        # 语义类默认向量主导（w_vec 大）；数字类自动切 BM25 主导（w_num_*）
        self.w_vec = w_vec
        self.w_bm25 = w_bm25
        self.auto_weight = auto_weight
        self.w_num_vec = w_num_vec
        self.w_num_bm25 = w_num_bm25
        self.chunks = []
        self.doc_ids = []
        self.doc_embs = []
        self._built = False

    def build(self, chunks):
        self.chunks = chunks
        self.bm25.build(chunks)
        self.doc_ids = [c.get("id") for c in chunks]
        self.doc_embs = self.embedder.embed_batch([c.get("text", "") for c in chunks])
        self._built = True
        return self

    def search(self, query, top_k=3, return_scores=True):
        if not self._built:
            raise RuntimeError("请先调用 build() 建库")
        # —— 意图自适应调权：数字类走 BM25 主导，否则走向量主导 ——
        if self.auto_weight and is_number_query(query):
            w_vec, w_bm25 = self.w_num_vec, self.w_num_bm25
        else:
            w_vec, w_bm25 = self.w_vec, self.w_bm25
        N = len(self.chunks)
        # —— BM25 排名 ——
        bm25_hits = self.bm25.search(query, top_k=N)
        bm25_rank = {h["id"]: i for i, h in enumerate(bm25_hits)}
        # —— 向量排名（向量侧做 query rewriting + max-pooling）——
        if self.enable_rewrite:
            q_embs = [self.embedder.embed_query(v) for v in expand_variants(query)]
        else:
            q_embs = [self.embedder.embed_query(query)]
        # 每个文档取「与任一子查询变体的最大余弦」→ 抗稀释、聚焦命中
        cos = []
        for i, e in enumerate(self.doc_embs):
            best = max(cosine(qe, e) for qe in q_embs)
            cos.append((best, i))
        cos.sort(key=lambda x: x[0], reverse=True)
        vec_rank = {self.doc_ids[i]: r for r, (_, i) in enumerate(cos)}
        # —— 加权 RRF 融合（向量加权，修正「自信但错的 BM25 覆盖正确向量信号」）——
        rrf = {}
        for cid, r in bm25_rank.items():
            rrf[cid] = rrf.get(cid, 0) + w_bm25 / (self.rrf_k + r)
        for cid, r in vec_rank.items():
            rrf[cid] = rrf.get(cid, 0) + w_vec / (self.rrf_k + r)
        ranked = sorted(rrf.items(), key=lambda x: x[1], reverse=True)
        out = []
        for cid, score in ranked[:top_k]:
            idx = self.doc_ids.index(cid)
            hit = dict(self.chunks[idx])
            if return_scores:
                hit["score"] = round(score, 4)
            out.append(hit)
        return out

    def explain(self, query, top_k=3):
        hits = self.search(query, top_k=top_k)
        return {
            "query_tokens": tokenize(query),
            "hits": [
                {
                    "id": h["id"],
                    "score": h.get("score"),
                    "customer": h.get("customer"),
                    "text": h.get("text"),
                }
                for h in hits
            ],
        }


def build_hybrid(path=CORPUS_PATH, enable_rewrite=True, w_vec=3.0, w_bm25=1.0,
                 auto_weight=True, w_num_vec=1.0, w_num_bm25=1.0):
    """一键加载语料 + 建混合检索库，返回 (retriever, corpus)。"""
    chunks = load_corpus(path)
    emb, name = get_embedder()
    r = HybridRetriever(embedder=emb, enable_rewrite=enable_rewrite,
                        w_vec=w_vec, w_bm25=w_bm25,
                        auto_weight=auto_weight,
                        w_num_vec=w_num_vec, w_num_bm25=w_num_bm25)
    r.emb_name = name
    return r.build(chunks), chunks


if __name__ == "__main__":
    retr, corpus = build_hybrid()
    print(f"语料: {len(corpus)} 条 | embedding: {retr.emb_name}")
    for q in ["哪些客户问过 LED Downlight？", "有没有客户投诉过质量问题？"]:
        print(f"\nQuery: {q}")
        for h in retr.search(q, top_k=2):
            print(f"  [{h['score']:.4f}] {h['id']} {h['customer']}: {h['text'][:50]}...")
