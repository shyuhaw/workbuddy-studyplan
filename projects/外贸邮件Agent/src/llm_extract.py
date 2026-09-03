# -*- coding: utf-8 -*-
"""
信息提取 —— LLM 兜底层 v1.0
==========================================
职责：只补齐规则层抽不到 / 抽不准的字段

与分类模块的关系：
- 复用 llm_fallback 的 provider 选择与配置（DeepSeek / OpenAI兼容 / 环境变量 / Mock）
- 复用 provider.raw_call() —— 拿到原始文本后自行解析（提取任务不是分类 JSON）

设计要点：
1. **字段级兜底**：只补缺失或低置信字段，规则层已高置信命中的不重复花钱
2. **诚实降级**：Mock 模式直接跳过，不做伪提取（Mock 没有真实语义理解能力）
3. **引用历史仲裁**：提示词明确要求"以最新确认数据为准"，这是规则层做不到的事
4. **可审计**：每次调用记 prompt/响应/耗时

作者：麦当
日期：2026-08-31
"""

import os
import json
import re
import time

from llm_fallback import FallbackManager, LOG_FILE
from classifier import LABEL_CN
from extractor import FIELDS

# 各字段的兜底阈值 —— 规则层在不同字段上的能力并不相同，阈值也应有别
FIELD_THRESHOLDS = {
    "customer": 0.7,
    "product": 0.85,   # 关键词只能给出"产品类别"，抽不到型号/规格 → 阈值更高，更容易转 LLM
    "quantity": 0.7,
    "price": 0.7,
    "deadline": 0.7,
}

# ---------------------------------------------------------------------------
# 分类驱动的字段语义 —— 整合后才可能做到
# ---------------------------------------------------------------------------
# 为什么需要它：
#   同一个字段名在不同类型邮件里含义不同。客诉邮件的"数量"指**缺陷数量**
#   （不是订单总量）；物流通知的"截止日"指**到港日 ETA**（不是已发生的开船日）。
#   不给这个上下文，LLM 会倾向于"把所有相关数字都列出来"，看似全面实则无用。
#   而"邮件是什么类型"这件事，只有先跑完分类才知道 —— 这正是整合的价值。
CATEGORY_FIELD_HINT = {
    "inquiry": ("数量 = 客户本次想采购的数量；"
                "价格 = 客户的目标价（邮件没给就填 null）；"
                "截止日 = 客户要求报价的最后期限。"),
    "order": ("数量 = 本次下单的数量；"
              "价格 = 成交单价；"
              "截止日 = 客户要求的交货期。"),
    "complaint": ("数量 = **出现异常/缺陷的那部分数量**，不是订单总量；"
                  "价格 = 索赔金额（没提赔偿就填 null）；"
                  "截止日 = 客户要求回复的期限。"),
    "notification": ("数量 = 单据所载货物数量；"
                     "价格 = null（通知类通常不涉及单价）；"
                     "截止日 = **最晚发生的那个时间节点**（例如到港日 ETA），"
                     "不要把已经发生的开船日 ETD 当作截止日。"),
}

SYSTEM_PROMPT = """你是外贸邮件结构化信息提取助手。
只输出一个 JSON 对象，不要输出任何解释、markdown 代码块标记或多余文字。"""


def build_extract_prompt(mail, rule_result, target_fields, category=""):
    """构造提取提示词（category 用于给出字段语义，实现"分类驱动提取"）"""
    subject = mail.get("subject", "")
    body = mail.get("body", "")
    from_addr = mail.get("from", "")

    rule_brief = {}
    for f in FIELDS:
        r = rule_result[f]
        rule_brief[f] = {
            "当前值": r["value"] if r["value"] is not None else None,
            "置信度": round(r["confidence"], 2),
            "候选数": len(r["candidates"]),
        }

    need = "\n".join(f"- {f}" for f in target_fields)

    hint = CATEGORY_FIELD_HINT.get(category, "")
    hint_block = ""
    if hint:
        hint_block = (f"\n【邮件类型】{LABEL_CN.get(category, category)}"
                      f" —— 请据此判断每个字段到底该取哪个值：\n{hint}\n")

    return f"""请从下面这封外贸邮件中提取结构化信息。

【邮件主题】{subject}
【发件人】{from_addr}
【邮件正文】
{body}

【需要重点补全的字段】（其余字段也请一并输出）
{need}
{hint_block}
【规则层已有结果，仅供参考，可能有误】
{json.dumps(rule_brief, ensure_ascii=False, indent=2)}

【字段定义】
- customer: 客户公司名称。优先取签名落款处的公司名，其次正文自称，最后才从邮箱域名推断。
- product: 产品名称（含型号；多个产品用 " / " 连接）
- quantity: 数量（含单位；多个用 " / " 连接，顺序与 product 对应）
- price: 单价（含币种和单位；多个用 " / " 连接；注意欧陆格式 EUR 8,90 表示 8.90 欧元）
- deadline: 截止/交货日期（保留原文表述）

【重要规则】
1. 邮件中确实未提供的字段，值填 null，**严禁编造**。例如邮件只说"请报价"而没给价格，price 必须是 null。
2. 若正文引用了历史邮件（形如 "On ... you wrote"、"--- Original message ---"、带引号的旧报价），
   **一律以发件人最新确认生效的数据为准**，忽略被引用的旧数字。
3. 多个产品时，product / quantity / price 三者内部用 " / " 连接，且保持一一对应的顺序。
4. 相对时间（如 end of next month、end of October）保留原文表述，不要臆测成具体日期。
5. 数量若只写了集装箱规格（如 40HQ）而无精确数字，quantity 填 null。
6. **每个字段只给一个最终值，不要把相关数字都列出来**。
   例如客诉邮件里同时出现订单总量和缺陷数量时，只取缺陷数量；
   同时出现开船日和到港日时，只取到港日。
   只有确实存在多个并列产品时，才用 " / " 连接。

输出 JSON（只输出这一个对象）：
{{"customer": "...", "product": "...", "quantity": "...", "price": "...", "deadline": "..."}}"""


def parse_extract_json(content):
    """解析 LLM 返回的提取 JSON，容错处理"""
    cleaned = (content or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    # 截取第一个 { 到最后一个 }
    s, e = cleaned.find("{"), cleaned.rfind("}")
    if s >= 0 and e > s:
        cleaned = cleaned[s:e + 1]
    data = json.loads(cleaned)
    return {f: (data.get(f) if data.get(f) not in ("", "null", "None") else None) for f in FIELDS}


class ExtractManager:
    """管理提取任务的 LLM 兜底调用"""

    def __init__(self, threshold=0.7):
        fm = FallbackManager()                      # 复用配置与 provider 选择逻辑
        self.provider = fm.provider
        self.is_mock = getattr(self.provider, "is_mock", False)
        self.threshold = threshold
        self.call_count = 0
        self.field_call_count = 0

    def need_fields(self, rule_result):
        """
        判定哪些字段需要 LLM 兜底：缺失 或 置信度低于该字段阈值。

        为什么分字段设阈值：
            规则层在各字段上的能力并不对等。产品字段依赖关键词匹配，
            命中 "acoustic ceiling panel" 只说明"知道是哪类产品"，
            但型号规格（600x600）抽不到 —— 这种 0.8 分是"部分正确"，
            若按 0.7 阈值就放过了，等于接受了不完整的答案。
            数量/价格/日期一旦唯一命中通常是准确的，可以放心采纳。
        """
        out = []
        for f in FIELDS:
            r = rule_result[f]
            th = FIELD_THRESHOLDS.get(f, self.threshold)
            if r["value"] is None or r["confidence"] < th:
                out.append(f)
        return out

    def extract(self, mail, rule_result, target_fields, category=""):
        """调用 LLM 补全目标字段，返回 (字段字典, 说明)"""
        if not target_fields:
            return {}, "无需兜底（规则层全部字段置信度达标）"

        if self.is_mock:
            return {}, "Mock 模式跳过（无真实 LLM，不做伪提取，保持结果诚实）"

        prompt = build_extract_prompt(mail, rule_result, target_fields, category)
        t0 = time.time()
        try:
            raw = self.provider.raw_call(SYSTEM_PROMPT, prompt)
            vals = parse_extract_json(raw)
            elapsed = time.time() - t0
            self.call_count += 1
            self.field_call_count += len(target_fields)
            self._log(mail.get("id", ""), prompt, raw, vals, elapsed, "ok")
            return vals, f"真实 LLM 补全（{len(target_fields)} 个字段，{elapsed:.1f}s）"
        except Exception as ex:
            elapsed = time.time() - t0
            self._log(mail.get("id", ""), prompt, f"[ERROR] {ex}", {}, elapsed, "error")
            return {}, f"LLM 调用失败，降级为规则层结果：{type(ex).__name__}: {ex}"

    def _log(self, mail_id, prompt, response, parsed, elapsed, status):
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "task": "extract",
                    "mail_id": mail_id,
                    "provider": getattr(self.provider, "name", "?"),
                    "is_mock": self.is_mock,
                    "prompt": prompt,
                    "response": response,
                    "parsed": parsed,
                    "elapsed": round(elapsed, 2),
                    "status": status,
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def summary(self):
        return {
            "provider": getattr(self.provider, "name", "none"),
            "is_mock": self.is_mock,
            "calls": self.call_count,
            "field_calls": self.field_call_count,
        }
