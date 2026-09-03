# -*- coding: utf-8 -*-
"""
Agent 耗时基准测试（P0 业务价值实证）
====================================
- 纯规则层耗时：classifier + extractor + decide（毫秒级，无 LLM）
- 完整 Agent 耗时：含真实 DeepSeek 兜底调用（秒级，受网络延迟主导）
- 输出 output/bench_agent.json，供 README / 作品集引用

用法：
    python src/bench_agent.py
"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
_PKGS = r"C:\Users\Administrator\.workbuddy\binaries\python\pkgs"
if _PKGS not in sys.path:
    sys.path.insert(0, _PKGS)

from classifier import score_email
from extractor import extract_fields, FIELDS
from agent import MailAgent, decide, BASE_DATE

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data", "e2e_emails.json")
OUT = os.path.join(BASE, "output", "bench_agent.json")

with open(DATA, encoding="utf-8") as f:
    emails = json.load(f)


def rules_only_one(m):
    cat, conf, scores, hits = score_email(m["subject"], m["body"])
    fields = extract_fields(m)
    decide(cat, {f: fields[f]["value"] for f in FIELDS}, m)


# ---- 纯规则层计时 ----
t0 = time.perf_counter()
for m in emails:
    rules_only_one(m)
t_rules = time.perf_counter() - t0

# ---- 完整 Agent（真实 LLM）计时 ----
agent = MailAgent()
t0 = time.perf_counter()
results = agent.process(emails)
t_real = time.perf_counter() - t0

n = len(emails)
bench = {
    "n_emails": n,
    "rules_only_sec": round(t_rules, 4),
    "rules_only_per_email_ms": round(t_rules / n * 1000, 2),
    "full_agent_sec": round(t_real, 3),
    "full_agent_per_email_sec": round(t_real / n, 3),
    "llm_time_sec_est": round(max(t_real - t_rules, 0), 3),
    "llm_calls": agent.fm.call_count + agent.em.call_count,
    "cls_calls": agent.fm.call_count,
    "ext_calls": agent.em.call_count,
    "provider": agent.em.provider.name,
    "is_mock": agent.em.is_mock,
}
os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(bench, f, ensure_ascii=False, indent=2)

print(json.dumps(bench, ensure_ascii=False, indent=2))
