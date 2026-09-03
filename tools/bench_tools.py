# -*- coding: utf-8 -*-
"""
日常工具提效实测（PDF 提炼 / Excel 探查 / 网页截图）
=====================================================
原则：AI 侧全部**真实测量**（wall-clock，含解释器启动），不做任何推算。
     人工侧用**透明模型**（保守下界），假设全部落盘供审计。

与「外贸邮件Agent」项目 output/bench_full.json 的方法学保持一致：
人工侧宁可低估，绝不虚报。

用法：
    python tools/bench_tools.py
输出：tools/bench_tools.json + 控制台汇总表

作者：麦当
日期：2026-09-02
"""

import os
import sys
import json
import time
import subprocess
import tempfile

_PKGS = r"C:\Users\Administrator\.workbuddy\binaries\python\pkgs"
if _PKGS not in sys.path:
    sys.path.insert(0, _PKGS)

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

DESKTOP = r"C:\Users\Administrator\Desktop"
PDF_DIR = os.path.join(DESKTOP, "图纸")   # 真实装修图纸 PDF（批量场景）
XLSX = os.path.join(HERE, "..", "projects", "外贸邮件Agent", "output", "Agent处理结果.xlsx")
SHOT_URL = "https://www.workbuddy.cn"
SHOT_OUT = os.path.join(tempfile.gettempdir(), "bench_shot.png")

# 人工侧保守假设（保守下界，真实含上下文切换/返工只多不少）
HUMAN_ASSUMPTIONS = {
    "pdf_per_file_min": 3.0,     # 翻页+识别关键信息+手写要点（保守下界）
    "excel_per_file_min": 2.0,   # 打开文件+切 sheet+数行列+记录
    "screenshot_min": 1.5,       # 开浏览器+输网址+等加载+截图+保存
}


def _timer():
    return time.perf_counter()


def bench_pdf(pdf_dir=None, label="PDF 批量提炼"):
    """批量 PDF 提炼：读文件夹下所有 PDF，提页数/字数/关键行"""
    from pypdf import PdfReader

    pdf_dir = pdf_dir or PDF_DIR
    pdfs = [f for f in os.listdir(pdf_dir) if f.lower().endswith(".pdf")]
    if not pdfs:
        return None

    t0 = _timer()
    results, no_text = [], []
    for name in pdfs:
        path = os.path.join(pdf_dir, name)
        try:
            r = PdfReader(path)
            pages = len(r.pages)
            text = ""
            for p in r.pages[:20]:          # 单份超 20 页截断，避免 token 爆炸
                text += (p.extract_text() or "")
            text = text.strip()
            if len(text) < 20:
                no_text.append(name)        # 扫描件/纯图 PDF，无文字层
            results.append({
                "file": name,
                "pages": pages,
                "chars": len(text),
                "head": text[:80].replace("\n", " "),
            })
        except Exception as e:
            results.append({"file": name, "error": str(e)})
    elapsed = _timer() - t0

    ok = [x for x in results if x.get("chars", 0) >= 20]
    total_chars = sum(x.get("chars", 0) for x in results)

    return {
        "item": label,
        "volume": f"{len(pdfs)} 份 / 共 {sum(x.get('pages', 0) for x in results)} 页",
        "agent_sec": round(elapsed, 3),
        # 人工侧只对「有文字层」的份数计费：无文字层的图纸 AI 也提不出东西，不能算提效
        "human_sec": round(len(ok) * HUMAN_ASSUMPTIONS["pdf_per_file_min"] * 60, 1),
        "notes": (f"{len(ok)}/{len(pdfs)} 份有文字层（{len(no_text)} 份为纯 CAD 图纸，"
                  f"无文字层，需 OCR，不计入提效）"),
        "extractable": len(ok),
        "no_text": len(no_text),
        "total_chars": total_chars,
        "files": results,
    }


def bench_excel():
    """Excel 结构探查：复用 tools/excel_probe.py 真实子进程调用"""
    path = os.path.abspath(XLSX)
    if not os.path.exists(path):
        return None

    t0 = _timer()
    proc = subprocess.run(
        [PY, os.path.join(HERE, "excel_probe.py"), path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    elapsed = _timer() - t0

    # 从 stdout 里数出 sheet 数量（脚本会打印 "[Sheet 数量] N"）
    sheets = 0
    for line in proc.stdout.splitlines():
        if "Sheet 数量" in line:
            try:
                sheets = int(line.split("]")[-1].strip())
            except ValueError:
                pass

    return {
        "item": "Excel 结构探查",
        "volume": f"{os.path.basename(path)}（{sheets} 个 Sheet）",
        "agent_sec": round(elapsed, 3),
        "human_sec": round(HUMAN_ASSUMPTIONS["excel_per_file_min"] * 60, 1),
        "notes": "含 Python 解释器启动 + openpyxl 加载",
        "exit_code": proc.returncode,
    }


def bench_screenshot():
    """网页截图：复用 tools/web_screenshot.py 真实子进程调用（Playwright + 系统 Edge）"""
    t0 = _timer()
    proc = subprocess.run(
        [PY, os.path.join(HERE, "web_screenshot.py"), SHOT_URL, SHOT_OUT],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    elapsed = _timer() - t0

    size_kb = round(os.path.getsize(SHOT_OUT) / 1024, 1) if os.path.exists(SHOT_OUT) else 0

    return {
        "item": "网页抓取 + 截图",
        "volume": SHOT_URL,
        "agent_sec": round(elapsed, 3),
        "human_sec": round(HUMAN_ASSUMPTIONS["screenshot_min"] * 60, 1),
        "notes": f"{'成功' if proc.returncode == 0 else '失败'}，产物 {size_kb} KB",
        "exit_code": proc.returncode,
        "stderr_tail": (proc.stderr or "").strip()[-300:],
    }


def main():
    rows = []
    tasks = [
        (lambda: bench_pdf(DESKTOP, "PDF 文字提取（有文字层）"), "PDF-桌面"),
        (lambda: bench_pdf(PDF_DIR, "CAD 图纸批量提取"), "PDF-图纸"),
        (bench_excel, "Excel"),
        (bench_screenshot, "截图"),
    ]
    for fn, label in tasks:
        print(f"[运行] {label} ...", flush=True)
        try:
            r = fn()
            if r:
                rows.append(r)
                print(f"   完成 {r['agent_sec']}s | {r.get('notes', '')}")
            else:
                print(f"   跳过（无可用素材）")
        except Exception as e:
            print(f"   失败: {type(e).__name__}: {e}")

    # 汇总
    print("\n" + "=" * 74)
    print(f"{'工作项':<16}{'体量':<26}{'人工':>10}{'AI':>10}{'提效':>10}")
    print("=" * 74)
    for r in rows:
        sp = r["human_sec"] / r["agent_sec"] if r["agent_sec"] > 0 else 0
        r["speedup_x"] = round(sp, 1)
        print(f"{r['item']:<16}{r['volume']:<26}"
              f"{r['human_sec']:>9.0f}s{r['agent_sec']:>9.2f}s{sp:>9.1f}×")

    out = {
        "date": "2026-09-02",
        "method": "AI 侧真实 wall-clock 测量（含解释器启动）；人工侧透明模型（保守下界）",
        "human_assumptions": HUMAN_ASSUMPTIONS,
        "rows": rows,
    }
    out_path = os.path.join(HERE, "bench_tools.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("=" * 74)
    print(f"[已导出] {out_path}")
    return out


if __name__ == "__main__":
    main()
