# -*- coding: utf-8 -*-
"""
外贸邮件信息提取 —— 规则层 v1.0
==========================================
从邮件中抽取 5 个结构化字段：客户 / 产品 / 数量 / 价格 / 截止日期

设计要点：
1. 正则 + 关键词 —— 零 API 成本、毫秒级、可解释（命中即知为什么）
2. 每个字段输出「值 + 置信度 + 候选数」，供上层做**字段级**兜底判定
3. 诚实处理缺失：抽不到就返回 None，绝不编造（'未提供'也是一种正确）
4. 多候选时主动降置信度 —— 承认"我可能配错了"，交给 LLM 仲裁

作者：麦当
日期：2026-08-31
"""

import re

FIELDS = ["customer", "product", "quantity", "price", "deadline"]

# ---------------------------------------------------------------------------
# 正则库
# ---------------------------------------------------------------------------
QTY_UNIT_EN = (r"pcs|pc|pieces|piece|units|unit|sets|set|sqm|square\s*meters?|meters?|"
               r"cartons?|ctns|boxes|box|kgs?|kilos?|tons?|rolls?|sheets?|panels?|containers?")
QTY_UNIT_CN = r"支|个|件|套|平方米|平米|公斤|千克|吨|箱|卷|张|台|米|块"
QTY_UNIT = rf"(?:{QTY_UNIT_EN}|{QTY_UNIT_CN})"

RE_QTY = re.compile(rf"(\d[\d,]*(?:\.\d+)?)\s*{QTY_UNIT}", re.I)

CURRENCY = r"USD|EUR|GBP|RMB|CNY|AUD|CAD|JPY"
# USD 12.50/pc  |  EUR 8,90 per meter  |  USD 6.80/sqm
RE_PRICE_CUR = re.compile(
    rf"({CURRENCY})\s?(\d[\d,]*(?:[.,]\d{{1,2}})?)(?:\s*(?:/|per)\s*({QTY_UNIT}))?", re.I)
# ¥580.00/张  |  $15.00
RE_PRICE_SYM = re.compile(
    rf"([\u00a5\uffe5$\u20ac\u00a3])\s?(\d[\d,]*(?:[.,]\d{{1,2}})?)(?:\s*/?\s*({QTY_UNIT}))?", re.I)
# 88元/支
RE_PRICE_CN = re.compile(
    rf"(\d[\d,]*(?:\.\d+)?)\s*(?:元|块钱)\s*/?\s*(?:每)?({QTY_UNIT_CN})?")

MONTHS = (r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
          r"Aug(?:ust)?|Sep(?:t)?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?")
RE_DATE_ISO = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}")
RE_DATE_DMY = re.compile(rf"(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTHS})\s+(\d{{4}})", re.I)
RE_DATE_MDY = re.compile(rf"({MONTHS})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})", re.I)
RE_DATE_CN = re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日")
# end of October 2026 / end of next month —— 相对时间，置信度低
RE_DATE_RELATIVE = re.compile(
    rf"end\s+of\s+(next\s+month|this\s+month|(?:{MONTHS})(?:\s+\d{{4}})?)", re.I)

COMPANY_SUFFIX = (r"(?:Pty\s+Ltd|Co\.,?\s*Ltd|Ltd|Limited|GmbH|Inc|LLC|Corp|Corporation|"
                  r"S\.?A\.?|B\.?V\.?|AB|AS|Oy|有限公司)")
RE_COMPANY = re.compile(rf"([A-Z][A-Za-z&\-\.\s]{{2,40}}?\s*{COMPANY_SUFFIX})")
RE_COMPANY_CN = re.compile(r"([\u4e00-\u9fa5]{2,20}(?:股份有限公司|有限公司))")
# 整行即公司名（签名区常见写法）——避免把人名/职位一起吞进来
RE_COMPANY_LINE = re.compile(
    rf"^\s*((?:[A-Z][\w&\-\.]*\s+){{0,4}}(?:{COMPANY_SUFFIX}))\.?\s*$")

# 职位/称谓词：全文退化匹配时用于剔除
JOB_TITLE_RE = re.compile(
    r"^(?:procurement|purchasing|sales|marketing|general|project|operations?|technical|"
    r"export|import|business|senior|junior|chief|head|manager|director|officer|president|"
    r"executive|engineer|ceo|cto|cfo|gm|md|mr|ms|mrs|dr)\s+", re.I)

PRODUCT_KEYWORDS = [
    "LED panel light", "LED downlight", "aluminum profile", "aluminium profile",
    "office workstation", "office furniture", "office chair", "ergonomic chair",
    "ceramic floor tile", "floor tile", "wall tile", "wall hung toilet",
    "basin mixer tap", "sanitary ware", "acoustic ceiling panel", "ceiling panel",
    "glass curtain wall", "carpet tile",
    "铝合金型材", "办公椅", "办公家具", "瓷砖", "天花吊顶板", "吸音板",
]
RE_ITEM_LABEL = re.compile(
    rf"(?:item|product|model|goods)\s*[:\-]\s*([^\n,;]{{3,60}})", re.I)


# ---------------------------------------------------------------------------
# 各字段提取
# ---------------------------------------------------------------------------
def _extract_quantity(text):
    """数量：数字 + 单位"""
    cands, seen = [], set()
    for m in RE_QTY.finditer(text):
        num = m.group(1)
        unit = m.group(0)[len(num):].strip()
        val = f"{num} {unit}".strip()
        # 归一化去重：主题里的 "3000 pcs" 与正文的 "3,000 pcs" 是同一个
        key = normalize(val)
        if key not in seen:
            seen.add(key)
            cands.append(val)
    if not cands:
        return None, 0.0, [], "未匹配到「数字+单位」模式"
    if len(cands) == 1:
        return cands[0], 0.9, cands, "唯一匹配"
    return " / ".join(cands), 0.6, cands, f"{len(cands)} 个候选，可能多产品或串行错配"


def _extract_price(text):
    """价格：币种符号/代码 + 数字（+ 单位）"""
    cands = []
    for m in RE_PRICE_CUR.finditer(text):
        cur, num, unit = m.group(1), m.group(2), m.group(3)
        cands.append(f"{cur} {num}" + (f"/{unit}" if unit else ""))
    for m in RE_PRICE_SYM.finditer(text):
        cands.append(f"{m.group(1)}{m.group(2)}" + (f"/{m.group(3)}" if m.group(3) else ""))
    for m in RE_PRICE_CN.finditer(text):
        cands.append(f"{m.group(1)}元" + (f"/{m.group(2)}" if m.group(2) else ""))

    # 去重保序
    seen, uniq = set(), []
    for c in cands:
        if c.lower() not in seen:
            seen.add(c.lower())
            uniq.append(c)

    if not uniq:
        return None, 0.0, [], "未匹配到价格模式"
    if len(uniq) == 1:
        return uniq[0], 0.9, uniq, "唯一匹配"
    return " / ".join(uniq), 0.6, uniq, f"{len(uniq)} 个候选，可能多产品或多轮报价"


def _extract_deadline(text):
    """截止日期：优先绝对日期，其次相对时间"""
    abs_cands = []
    abs_cands += RE_DATE_ISO.findall(text)
    abs_cands += [" ".join(m.group(0).split()) for m in RE_DATE_DMY.finditer(text)]
    abs_cands += [" ".join(m.group(0).split()) for m in RE_DATE_MDY.finditer(text)]
    abs_cands += [f"{a}年{b}月{c}日" for a, b, c in RE_DATE_CN.findall(text)]

    rel_cands = [" ".join(m.group(0).split()) for m in RE_DATE_RELATIVE.finditer(text)]

    if abs_cands and not rel_cands:
        if len(abs_cands) == 1:
            return abs_cands[0], 0.9, abs_cands, "唯一绝对日期"
        return " / ".join(abs_cands), 0.6, abs_cands, f"{len(abs_cands)} 个日期，需判别哪个是最新截止"
    if rel_cands:
        val = " / ".join(rel_cands)
        note = "相对时间表述，无法直接转绝对日期（需基准日期）"
        return val, 0.4, rel_cands, note
    if abs_cands:
        return " / ".join(abs_cands), 0.6, abs_cands, "绝对日期与相对时间并存，需仲裁"
    return None, 0.0, [], "未匹配到日期"


def _strip_job_titles(s):
    """剔除开头的职位/称谓词（如 'Procurement Manager '）"""
    prev = None
    while prev != s:
        prev = s
        s = JOB_TITLE_RE.sub("", s, count=1).strip()
    return s


def _extract_customer(from_addr, body):
    """
    客户：签名行整行匹配 > 全文匹配(剔除职位词) > 邮箱域名

    为什么优先"整行匹配"：
        签名区通常一行就是一个公司名（"Global Import Ltd."）。
        若直接在全文做贪婪匹配，会把人名和职位一起吞进去，得到
        "Michael Chen Procurement Manager Global Import Ltd." 这种脏值。
    """
    # 中文公司名
    cn = RE_COMPANY_CN.findall(body)
    if cn:
        return cn[-1], 0.9, cn, "正文匹配中文公司名"

    # 英文：逐行匹配（签名区干净，不带人名/职位）
    hits = []
    for ln in (l.strip() for l in body.split("\n")):
        if ln:
            m = RE_COMPANY_LINE.match(ln)
            if m:
                hits.append(m.group(1).strip())
    if hits:
        return hits[-1], 0.9, hits, "签名行整行匹配（无人名/职位污染）"

    # 退化：全文匹配后剔除职位词
    en = [" ".join(x.split()) for x in RE_COMPANY.findall(body)]
    cleaned = [c for c in (_strip_job_titles(x) for x in en) if c]
    if cleaned:
        return cleaned[-1], 0.7, cleaned, "全文匹配（已剔除职位词，置信度略低）"

    # 域名兜底
    m = re.search(r"@([\w\-\.]+)\.[a-z]{2,}", from_addr or "")
    if m:
        domain = m.group(1)
        guess = domain.replace("-", " ").replace(".", " ").title()
        return guess, 0.5, [domain], "仅能从邮箱域名推断，置信度低"
    return None, 0.0, [], "未匹配到客户信息"


def _extract_product(text):
    """产品：Item 标签 > 关键词库"""
    labeled = RE_ITEM_LABEL.findall(text)
    if labeled:
        cleaned = " ".join(labeled[0].split())
        return cleaned, 0.85, labeled, "匹配 Item/Product/Model 标签"

    hits = []
    low = text.lower()
    for kw in PRODUCT_KEYWORDS:
        if kw.lower() in low:
            hits.append(kw)
    # 子串去重：'floor tile' 若已被 'ceramic floor tile' 覆盖，只保留更具体的那个
    hits = [h for h in hits
            if not any(h is not o and h.lower() in o.lower() for o in hits)]
    if not hits:
        return None, 0.0, [], "未匹配到产品关键词"
    if len(hits) == 1:
        return hits[0], 0.8, hits, "关键词唯一命中"
    return " / ".join(hits), 0.6, hits, f"{len(hits)} 个产品关键词，可能多产品"


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def extract_fields(mail):
    """对单封邮件执行规则层提取，返回 {field: {value, confidence, ...}}"""
    from_addr = mail.get("from", "")
    subject = mail.get("subject", "")
    body = mail.get("body", "")
    text = f"{subject}\n{body}"

    q_v, q_c, q_cand, q_m = _extract_quantity(text)
    p_v, p_c, p_cand, p_m = _extract_price(text)
    d_v, d_c, d_cand, d_m = _extract_deadline(text)
    c_v, c_c, c_cand, c_m = _extract_customer(from_addr, body)
    pr_v, pr_c, pr_cand, pr_m = _extract_product(text)

    return {
        "customer": {"value": c_v, "confidence": c_c, "candidates": c_cand, "method": c_m},
        "product":  {"value": pr_v, "confidence": pr_c, "candidates": pr_cand, "method": pr_m},
        "quantity": {"value": q_v, "confidence": q_c, "candidates": q_cand, "method": q_m},
        "price":    {"value": p_v, "confidence": p_c, "candidates": p_cand, "method": p_m},
        "deadline": {"value": d_v, "confidence": d_c, "candidates": d_cand, "method": d_m},
    }


# ---------------------------------------------------------------------------
# 归一化（供命中判定，容忍表述差异）
# ---------------------------------------------------------------------------
def normalize(value, field=""):
    """归一化：去空格/符号/大小写，数字去掉千分位"""
    if not value:
        return ""
    s = str(value).strip().lower()
    s = re.sub(r"[\s\u3000]+", "", s)
    s = s.replace("，", ",").replace("。", "")
    # 数字：去掉千分位逗号（3位一组的）
    s = re.sub(r"(\d),(\d{3})(?!\d)", r"\1\2", s)
    # 小数逗号 → 点（欧陆格式 8,90 → 8.90）
    s = re.sub(r"(\d),(\d{1,2})(?!\d)", r"\1.\2", s)
    return s


def is_hit(extracted, expected, field=""):
    """
    判定提取是否命中（严格口径，防止"多候选蒙对"）。

    为什么严格：
        规则层常返回多个候选（如 E06 抽到 "1,200 pcs / 800 pcs"），
        其中虽然包含了正确答案，但系统**并没有确定下来是哪个**。
        若按"包含即命中"判定，等于给"含糊其辞"发奖励，会虚高准确率。
        所以含数字的字段要求**数字序列完全一致**才算命中。

    判定顺序：
    - 双方皆空 → True（识别"未提供"也正确；但性质属"缺失一致"，非提取能力）
    - 一方为空 → False
    - 含数字 → 数字序列必须完全一致
    - 纯文本（客户/产品）→ 包含匹配
    """
    e = normalize(extracted)
    x = normalize(expected)
    if not e and not x:
        return True
    if not e or not x:
        return False
    if e == x:
        return True
    en = re.findall(r"\d+(?:\.\d+)?", e)
    xn = re.findall(r"\d+(?:\.\d+)?", x)
    if en or xn:
        return en == xn
    return (x in e) or (e in x)
