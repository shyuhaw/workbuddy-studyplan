# -*- coding: utf-8 -*-
"""
外贸邮件 Agent · 可交互 Web Demo（零依赖 stdlib 服务端）
========================================================
让 HR / 面试官直接粘贴一封客户邮件，实时看到全链路：
    ① 分类  →  ② 关键信息提取  →  ③ 决策(优先级/动作/风险)  →  ④ RAG 检索历史  →  ⑤ 起草回复

为什么不用 Gradio/Streamlit：
    本项目整体是「零依赖」风格（自写 BM25、自写混合检索），且运行在隔离 Python 环境，
    装 Gradio/Streamlit 又重又有网络风险。用标准库 http.server 即可跑出可交互 Demo，
    本地零部署成本、可平移到任意端口 / 部署。

启动：  python src/demo_server.py [--port 7860]
接口：  GET  /                → 前端页面
        GET  /api/samples     → 返回真实 e2e 测试邮件（可一键载入）
        POST /api/run         → {"email": "<原始邮件文本>"} 跑全链路，返回 JSON
        GET  /api/health      → 健康检查（docker healthcheck / k8s readiness probe）
        GET  /api/metrics     → 运行时指标（调用次数 / token / 成本 / 错误率 / 最近请求列表）
        GET  /api/logs        → 最近 50 条 LLM 调用日志（jsonl 文件尾部）

作者：麦当
日期：2026-09-01
"""

import os
import sys
import re
import json
import time
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent import MailAgent, LABEL_CN
from vector_retriever import build_hybrid
from workflow import WorkflowCase
from llm_fallback import TOKEN_TOTAL, reset_token_total, LOG_FILE

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
E2E_PATH = os.path.join(BASE_DIR, "data", "e2e_emails.json")

print("[Demo] 正在加载 Agent 与混合检索器（含真实智谱向量，首次建库约数秒）...")
_AGENT = MailAgent()
_RETRIEVER, _CORPUS = build_hybrid()
_START_TIME = time.time()
_REQUEST_LOG = []  # 最近请求记录
print(f"[Demo] 就绪：检索器={_RETRIEVER.emb_name}  语料={len(_CORPUS)} 条")


# ---------------------------------------------------------------------------
# 邮件解析：从粘贴文本里拆出 subject / from / body
# ---------------------------------------------------------------------------
def parse_email(text):
    text = (text or "").strip()
    subject = from_addr = ""
    m = re.search(r"^Subject:\s*(.+)$", text, re.I | re.M)
    if m:
        subject = m.group(1).strip()
    m = re.search(r"^From:\s*(.+)$", text, re.I | re.M)
    if m:
        from_addr = m.group(1).strip()
    body_lines = []
    for ln in text.split("\n"):
        if re.match(r"^(Subject|From|To|Cc|Date):", ln, re.I):
            continue
        body_lines.append(ln)
    body = "\n".join(body_lines).strip()
    if not body:
        body = text
    return {"id": "DEMO", "from": from_addr,
            "subject": subject or "(无主题)", "body": body}


# ---------------------------------------------------------------------------
# 全链路执行
# ---------------------------------------------------------------------------
def run_pipeline(email_text):
    t0 = time.time()
    mail = parse_email(email_text)
    result = {"subject": mail["subject"]}
    try:
        res = _AGENT.process_one(mail)
        case = WorkflowCase(res, retriever=_RETRIEVER)
        case.run_pipeline()
        elapsed = round(time.time() - t0, 2)

        f = res["fields"]
        data = {
            "elapsed": elapsed,
            "llm_calls": _AGENT.fm.call_count + _AGENT.em.call_count,
            "input": {"subject": mail["subject"], "from": mail["from"]},
            "classification": {
                "category": res["category"],
                "category_cn": LABEL_CN.get(res["category"], res["category"]),
                "source": res["cat_source"],
                "need_llm": res["need_cls"],
                "confidence": res["rule_conf"],
                "rule_category": res["rule_cat"],
                "rule_category_cn": LABEL_CN.get(res["rule_cat"], res["rule_cat"]),
            },
            "extraction": {
                "fields": {k: {"value": v, "conf": (res["fields"].get(k) or "")}
                           for k, v in f.items()},
                "rule_fields": res["rule_fields"],
            },
            "decision": {
                "priority": res["priority"],
                "reasons": res["reasons"],
                "actions": res["actions"],
                "risks": res["risks"],
            },
            "rag": {
                "query": f"{f.get('customer') or ''} {f.get('product') or ''}".strip()
                         or mail["subject"],
                "hits": [
                    {"id": h.get("id"), "customer": h.get("customer"),
                     "text": (h.get("text") or "")[:160], "score": h.get("score")}
                    for h in case.retrieved
                ],
            },
            "draft": case.draft,
        }
        _REQUEST_LOG.append({
            "ts": time.strftime("%H:%M:%S"),
            "subject": mail["subject"],
            "elapsed": elapsed,
            "status": "ok"
        })
        return data
    except Exception as e:
        elapsed = round(time.time() - t0, 2)
        _REQUEST_LOG.append({
            "ts": time.strftime("%H:%M:%S"),
            "subject": mail["subject"],
            "elapsed": elapsed,
            "status": "error",
            "error": str(e)[:200]
        })
        raise


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        data = payload if isinstance(payload, (bytes, bytearray)) else payload.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "demo_frontend.html"), encoding="utf-8") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, json.dumps({"error": "frontend missing"}))
        elif path == "/api/samples":
            try:
                with open(E2E_PATH, encoding="utf-8") as fh:
                    data = json.load(fh)
                samples = [{"id": e["id"],
                            "subject": e.get("subject", ""),
                            "from": e.get("from", ""),
                            "body": e.get("body", ""),
                            "category": e.get("category", ""),
                            "note": e.get("note", "")}
                           for e in data]
                self._send(200, json.dumps({"samples": samples}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/health":
            self._send(200, json.dumps({"status": "ok",
                "corpus": len(_CORPUS),
                "retriever": _RETRIEVER.emb_name,
                "uptime_sec": round(time.time() - _START_TIME, 1)}, ensure_ascii=False))
        elif path == "/api/metrics":
            try:
                t = TOKEN_TOTAL or {}
                self._send(200, json.dumps({
                    "total_calls": t.get("calls", 0),
                    "prompt_tokens": t.get("prompt_tokens", 0),
                    "completion_tokens": t.get("completion_tokens", 0),
                    "total_cost_yuan": round((t.get("prompt_tokens", 0) * 1e-6
                                             + t.get("completion_tokens", 0) * 8e-6), 6),
                    "request_count": len(_REQUEST_LOG),
                    "recent": _REQUEST_LOG[-20:],
                }, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/logs":
            try:
                lines = []
                if os.path.exists(LOG_FILE):
                    with open(LOG_FILE, "r", encoding="utf-8") as fh:
                        for ln in fh.readlines()[-50:]:
                            try:
                                lines.append(json.loads(ln))
                            except Exception:
                                pass
                self._send(200, json.dumps({"logs": lines, "count": len(lines)}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if urlparse(self.path).path != "/api/run":
            self._send(404, json.dumps({"error": "not found"}))
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(raw)
            email = payload.get("email", "")
            if not email.strip():
                self._send(400, json.dumps({"error": "email 为空"}))
                return
            result = run_pipeline(email)
            self._send(200, json.dumps(result, ensure_ascii=False))
        except Exception as e:
            self._send(500, json.dumps({"error": str(e)}))

    def log_message(self, *args):
        pass  # 静默访问日志


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()
    srv = HTTPServer((args.host, args.port), Handler)
    print(f"[Demo] 服务已启动 → http://{args.host}:{args.port}/")
    print("[Demo] 按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[Demo] 已停止")
        srv.shutdown()


if __name__ == "__main__":
    main()
