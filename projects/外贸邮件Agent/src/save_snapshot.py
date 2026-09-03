# -*- coding: utf-8 -*-
"""
把 Agent Mail 的 GetMessage 原始输出，转成标准快照 JSON。

为什么需要它：
    之前 make_snapshot.py 是把邮件正文硬编码进脚本的，那只是演示用的一次性搬运。
    真正要用起来，必须能"拿到什么就存什么"——本脚本就是干这个的：
    把 MCP 返回的原始 JSON 直接落盘，不做任何内容加工（清洗交给 mail_connector）。

用法：
    python save_snapshot.py <mcp_raw.json> [--out data/inbox_snapshot.json]
    python save_snapshot.py <mcp_raw.json> --append      # 追加而非覆盖

输入格式兼容：
    - MCP 返回的完整结构 {"data": {"data": {...}}}
    - 裸的消息对象 {...}
    - 消息数组 [{...}, {...}]
"""

import os
import sys
import json
import argparse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(BASE, "data", "inbox_snapshot.json")


def extract_messages(raw):
    """从各种嵌套结构里把消息列表挖出来"""
    node = raw
    # 逐层下钻 {"data": {...}}
    while isinstance(node, dict) and isinstance(node.get("data"), (dict, list)):
        node = node["data"]
    if isinstance(node, dict):
        return [node]
    if isinstance(node, list):
        return node
    raise ValueError("无法从输入中解析出邮件列表")


def normalize(m, index):
    """把一条 MCP 消息映射成快照格式"""
    frm = m.get("from", "")
    if isinstance(frm, dict):
        name = frm.get("name", "")
        email = frm.get("email", "")
        frm = f"{name} <{email}>" if name else email

    to_list = m.get("to", [])
    if isinstance(to_list, list):
        to_str = ", ".join(
            (t.get("email", "") if isinstance(t, dict) else str(t)) for t in to_list
        )
    else:
        to_str = str(to_list)

    return {
        "id": f"M{index:03d}",
        "message_id": m.get("message_id", ""),
        "from": frm,
        "to": to_str,
        "subject": m.get("subject", ""),
        "body": m.get("body", "") or m.get("snippet", ""),
        "created_at": m.get("created_at", ""),
    }


def main():
    ap = argparse.ArgumentParser(description="保存邮件快照")
    ap.add_argument("raw", help="MCP GetMessage 输出的 JSON 文件")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--append", action="store_true", help="追加到已有快照")
    args = ap.parse_args()

    with open(args.raw, "r", encoding="utf-8") as f:
        raw = json.load(f)

    items = extract_messages(raw)
    new = [normalize(m, i) for i, m in enumerate(items, 1)]

    if args.append and os.path.exists(args.out):
        with open(args.out, "r", encoding="utf-8") as f:
            old = json.load(f)
        # 按 message_id 去重
        seen = {x.get("message_id") for x in old}
        new = [x for x in new if x["message_id"] not in seen]
        merged = old + [normalize(x, len(old) + i + 1)
                        for i, x in enumerate(new)]
    else:
        merged = new

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    print(f"[已保存] {args.out}")
    print(f"邮件数: {len(merged)}（本次新增 {len(new)}）")
    for m in merged:
        print(f"  {m['id']}  {len(m['body'])} 字符  |  {m['subject'][:46]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
