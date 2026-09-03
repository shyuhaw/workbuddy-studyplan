# -*- coding: utf-8 -*-
"""
网页截图工具（调用系统已装的 Edge / Chrome，无需下载 Chromium）
用法: python web_screenshot.py <URL> [输出路径]
默认输出: 桌面 web_screenshot.png
"""
import sys
import os

_PKGS = r"C:\Users\Administrator\.workbuddy\binaries\python\pkgs"
if _PKGS not in sys.path:
    sys.path.insert(0, _PKGS)

from playwright.sync_api import sync_playwright

DEFAULT_OUT = r"C:\Users\Administrator\Desktop\web_screenshot.png"


def shoot(url, out_path, width=1440, height=900):
    with sync_playwright() as p:
        # channel="msedge" 复用系统 Edge，避免下载 Chromium(~500MB)
        browser = p.chromium.launch(
            channel="msedge",
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        page = browser.new_page(viewport={"width": width, "height": height})
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)  # 等页面渲染
        page.screenshot(path=out_path)
        title = page.title()
        browser.close()
    return title


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "https://www.baidu.com"
    out_path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUT

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    print(f"[目标] {url}")
    title = shoot(url, out_path)
    size_kb = os.path.getsize(out_path) / 1024
    print(f"[标题] {title}")
    print(f"[已保存] {out_path}  ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
