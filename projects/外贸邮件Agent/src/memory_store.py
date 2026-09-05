# -*- coding: utf-8 -*-
"""
跨轮记忆存储（JD #2 最后一个能力：记忆）
========================================

为什么需要它
------------
`ToolSession` 只存「一次运行」的中间结果（retrieve 的 hits、build_context 的产物）。
跑完一封邮件，上下文就丢了。下一封同客户的邮件进来，Agent 像失忆一样从头开始。

但真实外贸业务是**有续集的**：
    E07  Canada Decor 续单 PO-2026-0432，1800 平，11/20 前
    E17  Canada Decor 又提 PO-2026-0432，但 2000 平、12/15 前  ← 数量/交期变了
没有跨轮记忆，Agent 处理 E17 时不知道「这客户上次订的什么、合同价多少、交期改过几次」。

本模块 = Agent 的「工作记忆」（working memory）
----------------------------------------------
- 存的是 Agent 自己处理过程中**学到的、关于客户/线索的事实**：合同规格、偏好、未决问题、联系人。
- 与 RAG 语料库（customer_corpus.json，权威交易史）**严格隔离**：
  记忆可能过时/有误，权威事实仍必须 retrieve_history 检索校验。这是边界，不是缺陷。
- 检索用「关键词重叠」打分（零重依赖，不装 torch/BGE），适合短工作记忆；
  升级路径是向量召回（README 已注明）。

持久化：可选落盘 JSON。内存态 + 文件备份。

作者：麦当 · 2026-09-04（Day06 续 · 跨轮记忆）
"""

import json
import os
import re
import time


def _tokens(text):
    """轻量分词：英文/数字词 + 单个汉字。不依赖任何 NLP 库。"""
    text = (text or "").lower()
    toks = set(re.findall(r"[a-z0-9]+", text))
    toks |= set(re.findall(r"[\u4e00-\u9fff]", text))
    toks.discard("")
    return toks


class MemoryStore:
    """跨轮工作记忆存储。支持按客户/关键词召回，可选落盘。"""

    def __init__(self, path=None):
        self.path = path
        self.entries = []  # [{id, customer, fact_type, fact, mail_id, ts}]
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.entries = json.load(f)
            except Exception:
                self.entries = []

    # -- 写 --
    def remember(self, customer, fact_type, fact, mail_id=None):
        """存一条事实。返回新写入的 entry。"""
        eid = f"M{len(self.entries) + 1:04d}"
        entry = {
            "id": eid,
            "customer": customer or "",
            "fact_type": fact_type or "note",
            "fact": fact or "",
            "mail_id": mail_id or "",
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.entries.append(entry)
        self.save()
        return entry

    # -- 读 --
    def recall(self, customer=None, query=None, limit=5):
        """召回相关记忆。先按客户过滤，再按关键词重叠打分排序。"""
        cand = self.entries
        if customer:
            cand = [e for e in cand if e["customer"] == customer]
        if query:
            q = _tokens(query)
            scored = []
            for e in cand:
                blob = f"{e['fact']} {e.get('fact_type', '')} {e.get('customer', '')}"
                score = len(q & _tokens(blob))
                if score > 0:
                    scored.append((score, e))
            scored.sort(key=lambda x: -x[0])
            cand = [e for _, e in scored[:limit]]
        return cand

    def get_customer(self, customer):
        """某客户的全量记忆（按时间序）。"""
        return [e for e in self.entries if e["customer"] == customer]

    def summary(self, customer=None):
        """人读摘要。"""
        rows = self.get_customer(customer) if customer else self.entries
        if not rows:
            return "（记忆为空）"
        return "\n".join(
            f"- [{e['fact_type']}] {e['fact']}（来源:{e['mail_id'] or '—'} @ {e['ts']}）"
            for e in rows
        )

    # -- 持久化 --
    def save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.entries, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def reset(self):
        self.entries = []
        if self.path and os.path.exists(self.path):
            try:
                os.remove(self.path)
            except Exception:
                pass
