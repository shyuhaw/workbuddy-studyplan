# -*- coding: utf-8 -*-
"""
LLM 兜底层 v1.0
================================
职责：处理规则层"拿不准"的邮件（置信度低于阈值）

设计原则：
1. **可插拔**：支持 DeepSeek / OpenAI / 智谱 / Mock，切换只改配置
2. **优雅降级**：没有 API Key 时不崩溃，自动降级到 Mock + 标记待人工
3. **可审计**：每次兜底调用都记录 prompt / 响应 / 耗时，便于复盘和调优
4. **成本可控**：只对低置信度邮件调用，正常情况 80%+ 的邮件不花钱

作者：麦当
日期：2026-08-31
"""

import os
import sys
import json
import time
from abc import ABC, abstractmethod

_PKGS = r"C:\Users\Administrator\.workbuddy\binaries\python\pkgs"
if _PKGS not in sys.path:
    sys.path.insert(0, _PKGS)

import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(BASE_DIR, "config", "llm_config.json")
LOG_FILE = os.path.join(BASE_DIR, "output", "llm_calls.jsonl")

LABEL_CN = {
    "inquiry": "询盘",
    "order": "订单",
    "complaint": "投诉",
    "notification": "通知",
}

SYSTEM_PROMPT = """你是一个外贸邮件分类专家。请将客户邮件归类到以下四类中的一类：

- inquiry（询盘）：客户还在问价、问规格、问交期、索要样品，尚未决定购买
- order（订单）：客户已确认要买，包括下单、修改订单、追加数量、变更规格
- complaint（投诉）：客户反馈问题，包括质量缺陷、货损、短缺、延误、索赔
- notification（通知）：被动告知信息，不需要收件方采取行动，如发货通知、付款通知、清关通知

判断要点：
1. 看**主意图**，不要被个别词带偏。一封邮件可能提到多个主题，取最主要的那个
2. 注意**否定和转折**，例如 "this is not a complaint" 说明不是投诉
3. 语气客气不等于询盘。投诉邮件也可能写得很礼貌
4. 已下单后询问进度，属于 inquiry（在问，不在买）

只输出 JSON，不要有其他文字：
{"category": "四选一", "reason": "一句话说明判断依据", "confidence": 0-1之间的小数}
"""


def build_prompt(subject, body, rule_scores):
    """构造用户 prompt，把规则层的结果也告诉 LLM 作为参考"""
    scores_text = "、".join(
        f"{LABEL_CN[k]}({k})={v}分" for k, v in sorted(rule_scores.items(), key=lambda x: -x[1])
    )
    return f"""邮件主题：{subject}

邮件正文：
{body}

---
规则引擎初步打分：{scores_text}
（仅供参考，请独立判断，不要盲从规则引擎）

请分类并输出 JSON。"""


# ---------------------------------------------------------------------------
# Provider 抽象
# ---------------------------------------------------------------------------
class BaseProvider(ABC):
    """所有 LLM Provider 的统一接口"""

    name = "base"
    is_mock = False

    @abstractmethod
    def call(self, system_prompt, user_prompt):
        """返回 (category, reason, confidence)"""
        raise NotImplementedError


class DeepSeekProvider(BaseProvider):
    """DeepSeek —— 性价比最高，约 1 元/百万 token"""

    name = "deepseek"
    endpoint = "https://api.deepseek.com/chat/completions"
    model = "deepseek-chat"

    def __init__(self, api_key, timeout=30):
        self.api_key = api_key
        # timeout 可调：生成端（RAG）要求 5s 内返回否则降级，分类/提取沿用 30s
        self.timeout = timeout

    def raw_call(self, system_prompt, user_prompt):
        """调用 API 返回原始文本（不做业务解析）—— 供分类/提取等不同下游复用"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        resp = requests.post(self.endpoint, headers=headers, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def call(self, system_prompt, user_prompt):
        return parse_llm_json(self.raw_call(system_prompt, user_prompt))


class OpenAICompatProvider(BaseProvider):
    """OpenAI 兼容接口（OpenAI / 智谱 / 月之暗面 / 通义 等）"""

    name = "openai_compat"

    def __init__(self, api_key, endpoint, model):
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model

    def raw_call(self, system_prompt, user_prompt):
        """调用 API 返回原始文本（不做业务解析）—— 供分类/提取等不同下游复用"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
        }
        resp = requests.post(self.endpoint, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def call(self, system_prompt, user_prompt):
        return parse_llm_json(self.raw_call(system_prompt, user_prompt))


class MockProvider(BaseProvider):
    """
    模拟 LLM —— 无 API Key 时使用

    诚实说明：这是"更精细的规则引擎"，不是真正的语义理解。
    它能跑通完整流程、验证架构，但**不能替代真实 LLM**。
    配置 API Key 后会自动切换到真实模型。
    """

    name = "mock"
    is_mock = True

    # 主意图信号（比原始关键词更能反映真实意图）
    STRONG_SIGNALS = {
        "order": [
            "we'd like to proceed", "let's place the order", "place an order",
            "decided to move forward", "prepare the proforma invoice",
            "send the proforma invoice", "postpone the shipment",
            "request to postpone", "samples approved",
        ],
        "complaint": [
            "defective", "found some issues", "file a formal claim",
            "compensation policy", "resolve this",
        ],
        "inquiry": [
            "haven't received any", "could you please update",
            "when can we expect", "advise when",
        ],
        "notification": [
            "we have transferred", "payment sent", "please confirm receipt",
            "remittance reference",
        ],
    }

    # 否定信号：出现则降低对应类别的可信度
    NEGATIONS = {
        "complaint": ["not a complaint", "not complaining", "no complaint"],
    }

    def call(self, system_prompt, user_prompt):
        text = user_prompt.lower()

        scores = {}
        for cat, signals in self.STRONG_SIGNALS.items():
            scores[cat] = sum(1 for s in signals if s in text)

        # 处理否定
        for cat, negs in self.NEGATIONS.items():
            if any(n in text for n in negs):
                scores[cat] = 0

        best = max(scores, key=scores.get)
        total = sum(scores.values())
        conf = (scores[best] / total) if total > 0 else 0.5
        conf = min(conf, 0.95)

        reasons = {
            "order": "邮件主意图是推进订单（下单/修改/延期），虽有询问但服务于订单执行",
            "complaint": "邮件在反馈已发生的问题并讨论索赔，语气客气不改变性质",
            "inquiry": "邮件在追问已有订单的进度，属于询问而非下单",
            "notification": "邮件主要目的是告知已完成付款，附带询问是次要的",
        }

        return best, reasons.get(best, "综合判断"), conf


def parse_llm_json(content):
    """解析 LLM 返回的 JSON，容错处理"""
    try:
        # 去掉可能的 markdown 代码块
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```")[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        data = json.loads(cleaned)
        cat = data.get("category", "").strip().lower()
        if cat not in LABEL_CN:
            raise ValueError(f"未知分类: {cat}")
        return cat, data.get("reason", ""), float(data.get("confidence", 0.8))
    except Exception as e:
        raise ValueError(f"LLM 返回解析失败: {e} | 原始内容: {content[:200]}")


# ---------------------------------------------------------------------------
# 兜底管理器
# ---------------------------------------------------------------------------
class FallbackManager:
    """管理 LLM 兜底调用，含日志和降级"""

    def __init__(self, threshold=0.8, provider=None):
        self.threshold = threshold
        self.provider = provider or self._auto_select_provider()
        self.call_count = 0
        self.total_cost_estimate = 0.0

    def _auto_select_provider(self):
        """自动选择 provider：有配置用真实 API，无配置用 Mock"""
        cfg = self._load_config()

        if cfg.get("deepseek", {}).get("api_key"):
            return DeepSeekProvider(cfg["deepseek"]["api_key"])

        oc = cfg.get("openai_compat", {})
        if oc.get("api_key") and oc.get("endpoint"):
            return OpenAICompatProvider(oc["api_key"], oc["endpoint"], oc.get("model", "gpt-4o-mini"))

        # 环境变量兜底
        env_key = os.environ.get("DEEPSEEK_API_KEY")
        if env_key:
            return DeepSeekProvider(env_key)

        return MockProvider()

    @staticmethod
    def _load_config():
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def need_fallback(self, confidence, scores=None, dispute_ratio=0.6):
        """
        是否需要 LLM 兜底 —— 两条触发路径：

        1. 置信度低于阈值（拿不准 → 交给 LLM）
        2. **争议度检测**：即便置信度很高，若第一名与第二名得分接近，
           说明规则层其实"分不出来"，只是某个词多命中了几次。
           典型场景：客户回复报价邮件说 "We accept... please proceed"，
           既像询盘又像订单，规则层给了 80%+ 但判错。

        第 2 条补的是阈值的盲区：**阈值只救"不自信的错"，救不了"自信但错"**。
        """
        if confidence < self.threshold:
            return True

        if scores:
            ranked = sorted((v for v in scores.values() if v > 0), reverse=True)
            if len(ranked) >= 2 and ranked[0] > 0 and (ranked[1] / ranked[0]) >= dispute_ratio:
                return True

        return False

    def classify(self, subject, body, rule_scores):
        """调用 LLM 兜底，返回 (category, reason, confidence)"""
        user_prompt = build_prompt(subject, body, rule_scores)
        start = time.time()

        try:
            cat, reason, conf = self.provider.call(SYSTEM_PROMPT, user_prompt)
            elapsed = time.time() - start
            self.call_count += 1
            self._log(subject, user_prompt, cat, reason, conf, elapsed, success=True)
            return cat, reason, conf
        except Exception as e:
            elapsed = time.time() - start
            self._log(subject, user_prompt, None, str(e), 0, elapsed, success=False)
            # 降级：返回规则层的最高分结果，并标记
            fallback_cat = max(rule_scores, key=rule_scores.get)
            return fallback_cat, f"[兜底失败，降级为规则层结果] {e}", 0.0

    def _log(self, subject, prompt, cat, reason, conf, elapsed, success):
        """记录每次调用，便于复盘和成本分析"""
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        record = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "provider": self.provider.name,
            "is_mock": self.provider.is_mock,
            "subject": subject[:60],
            "result": cat,
            "reason": reason,
            "confidence": conf,
            "elapsed_sec": round(elapsed, 2),
            "success": success,
        }
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def summary(self):
        return {
            "provider": self.provider.name,
            "is_mock": self.provider.is_mock,
            "calls": self.call_count,
        }
