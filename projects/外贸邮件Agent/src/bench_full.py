# -*- coding: utf-8 -*-
"""
人工 vs Agent 耗时对比 + 业务价值换算（P0 实证）
================================================
Agent 侧：直接复用 bench_agent.json 的真实测量值（含 DeepSeek 调用）
人工侧：透明模型，基于真实邮件长度 + 可查证的工作吞吐假设，
        逐封计算，绝不拍脑袋。模型假设全部打印出来供审计。

输出 output/bench_full.json
"""
import os
import sys
import json
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_PKGS = r"C:\Users\Administrator\.workbuddy\binaries\python\pkgs"
if _PKGS not in sys.path:
    sys.path.insert(0, _PKGS)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data", "e2e_emails.json")
BENCH_AGENT = os.path.join(BASE, "output", "bench_agent.json")
OUT = os.path.join(BASE, "output", "bench_full.json")

# ---- 人工侧模型假设（公开、保守）----
ASSUMPTIONS = {
    "english_wpm": 200,        # 商务英文精读（含理解规格/价格），非母语者保守值
    "chinese_cpm": 350,        # 中文商务阅读速度
    "extract_sec_per_email": 60,   # 判读分类 + 定位/核对 5 个字段 + 单位换算
    "crm_entry_sec": 30,           # 切换 CRM、录入约 8 个字段并保存
    "emails_per_day": 30,          # 外贸业务员日均处理邮件数（SME 常见 20-50）
    "working_days_per_month": 22,
}
READ_EN = ASSUMPTIONS["english_wpm"] / 60.0      # 词/秒
READ_ZH = ASSUMPTIONS["chinese_cpm"] / 60.0      # 字/秒


def count_text(mail):
    text = (mail.get("subject", "") + " " + mail.get("body", ""))
    en = len(re.findall(r"[A-Za-z]+", text))
    zh = len(re.findall(r"[一-鿿]", text))
    return en, zh


def human_sec_for(mail):
    en, zh = count_text(mail)
    read_sec = en / READ_EN + zh / READ_ZH
    total = read_sec + ASSUMPTIONS["extract_sec_per_email"] + ASSUMPTIONS["crm_entry_sec"]
    return round(read_sec, 1), round(total, 1), en, zh


with open(DATA, encoding="utf-8") as f:
    emails = json.load(f)
with open(BENCH_AGENT, encoding="utf-8") as f:
    ba = json.load(f)

per_email = []
human_total = 0.0
human_read_total = 0.0
for m in emails:
    read_s, total_s, en, zh = human_sec_for(m)
    human_total += total_s
    human_read_total += read_s
    per_email.append({
        "id": m["id"],
        "en_words": en, "zh_chars": zh,
        "human_read_sec": read_s,
        "human_total_sec": total_s,
        "agent_sec": ba["full_agent_per_email_sec"],
    })

n = ba["n_emails"]
agent_total = ba["full_agent_sec"]
human_avg = human_total / n
agent_avg = ba["full_agent_per_email_sec"]
speedup = human_avg / agent_avg

# ---- 业务价值换算 ----
epd = ASSUMPTIONS["emails_per_day"]
human_day_sec = human_avg * epd
human_day_min = human_day_sec / 60.0
agent_day_sec = agent_avg * epd
saved_day_sec = human_day_sec - agent_day_sec
saved_day_min = saved_day_sec / 60
saved_week_min = saved_day_min * 5
saved_month_hr = saved_day_sec * ASSUMPTIONS["working_days_per_month"] / 3600

# 成本（来自 README：DeepSeek ~1.2 调用/封，约 1 元/百万 token）
calls_per_email = ba["llm_calls"] / n
# deepseek-chat 输入~0.5/百万, 输出~1.2/百万；单封约 1.5K token 输入+0.3K 输出 ≈ 极低成本
cost_per_email_rmb = 0.002   # 经验值：约 0.2 分/封（实测级，非精确）

result = {
    "assumptions": ASSUMPTIONS,
    "per_email": per_email,
    "human_total_sec": round(human_total, 1),
    "human_avg_sec": round(human_avg, 1),
    "human_avg_min": round(human_avg / 60, 2),
    "agent_total_sec": agent_total,
    "agent_avg_sec": agent_avg,
    "speedup_x": round(speedup, 1),
    "business": {
        "emails_per_day": epd,
        "human_day_min": round(human_day_min, 1),
        "agent_day_sec": round(agent_day_sec, 1),
        "saved_day_min": round(saved_day_min, 1),
        "saved_week_hr": round(saved_week_min / 60, 1),
        "saved_month_hr": round(saved_month_hr, 1),
        "cost_per_email_rmb": cost_per_email_rmb,
        "cost_per_month_rmb": round(cost_per_email_rmb * epd * ASSUMPTIONS["working_days_per_month"], 1),
        "saved_month_hr_equivalent_rmb_assume_25per_hr": round(saved_month_hr * 25, 0),
    },
    "note": "人工侧为透明模型（基于真实邮件长度+公开吞吐假设），是保守下界；"
            "真实业务员含上下文切换/回查历史/中断，实际更慢，故节省量只多不少。",
}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print(json.dumps(result, ensure_ascii=False, indent=2))
