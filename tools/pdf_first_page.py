# -*- coding: utf-8 -*-
"""
PDF 第一页文字提取器
用法: python pdf_first_page.py [文件夹路径]
用途: WorkBuddy Day01 能力验证 + 后续批量处理的基础工具
"""
import sys
import glob
import os

# 依赖装在隔离目录，脚本自包含，无需配置 PYTHONPATH
_PKGS = r"C:\Users\Administrator\.workbuddy\binaries\python\pkgs"
if _PKGS not in sys.path:
    sys.path.insert(0, _PKGS)

from pypdf import PdfReader

# 默认读取桌面的报销发票文件夹，可通过命令行参数覆盖
DEFAULT_FOLDER = r"C:\Users\Administrator\Desktop\10.21章唯报销\浙C661YJ (2)"

def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FOLDER

    pdfs = sorted(glob.glob(os.path.join(folder, "*.pdf")))
    if not pdfs:
        print(f"[!] 该文件夹下没有找到 PDF: {folder}")
        return 1

    target = pdfs[0]
    print(f"[文件] {os.path.basename(target)}")
    print(f"[路径] {target}")
    print(f"[该文件夹共 {len(pdfs)} 个 PDF]")

    reader = PdfReader(target)
    print(f"[总页数] {len(reader.pages)}")
    print("=" * 50)
    print("【第 1 页内容】")
    print("=" * 50)
    print(reader.pages[0].extract_text())

    return 0

if __name__ == "__main__":
    sys.exit(main())
