# -*- coding: utf-8 -*-
"""
构建端到端测试集 e2e_emails.json
==========================================
背景：分类模块和提取模块此前各用各的数据集（sample/ambiguous vs extract），
整合后需要**同一批数据**同时支撑两个任务的验证，否则无法证明端到端效果。

做法：
1. 给已有 8 封提取样本补上分类标注
2. 新增投诉类、通知类各 1 封（原 8 封只覆盖询盘/订单两类）
3. 输出 e2e_emails.json
"""

import os
import json
from collections import Counter

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "data", "extract_emails.json")
DST = os.path.join(BASE, "data", "e2e_emails.json")

# 已有 8 封的分类标注（人工判定）
CAT = {
    "E01": "inquiry", "E02": "inquiry", "E03": "inquiry", "E04": "order",
    "E05": "inquiry", "E06": "order", "E07": "order", "E08": "order",
}

# 新增两封，补齐"投诉"和"通知"两个类别
EXTRA = [
    {
        "id": "E09",
        "from": "complaints@us-hotelgroup.com",
        "subject": "Complaint - defective LED panels, Order PO-2026-0398",
        "body": ("Dear Sir,\n\n"
                 "We received 2,400 pcs of LED panel light (PL-6060) under PO-2026-0398 on 2026-08-20.\n\n"
                 "Unfortunately, 180 pcs show obvious color temperature deviation "
                 "(6000K instead of 4000K). Photos are attached.\n\n"
                 "We request either replacement of the defective units or compensation of USD 3,600.\n"
                 "Please reply before 2026-09-05.\n\n"
                 "Robert Turner\n"
                 "Operations Manager\n"
                 "US Hotel Group LLC"),
        "category": "complaint",
        "expected": {
            "customer": "US Hotel Group LLC",
            "product": "LED panel light PL-6060",
            "quantity": "180 pcs",
            "price": "USD 3,600",
            "deadline": "2026-09-05"
        },
        "note": "投诉类陷阱：数量应是**缺陷数量 180 pcs**而非订单总量 2,400 pcs，"
                "价格是**索赔额**而非单价——测试 LLM 能否结合语境理解字段含义"
    },
    {
        "id": "E10",
        "from": "shipping@cosco-logistics.com",
        "subject": "Shipping Advice - B/L No. COSU6123456789",
        "body": ("Dear Customer,\n\n"
                 "We are pleased to advise that the following shipment has been dispatched:\n\n"
                 "Order ref: PO-2026-0512\n"
                 "Goods: Ceramic floor tile 600x600mm\n"
                 "Quantity: 1,680 cartons\n"
                 "Vessel: MV PACIFIC STAR / V.231E\n"
                 "ETD Ningbo: 2026-08-28\n"
                 "ETA Felixstowe: 2026-09-25\n"
                 "B/L No.: COSU6123456789\n\n"
                 "Please find attached the draft bill of lading for your confirmation.\n\n"
                 "COSCO Logistics Co., Ltd."),
        "category": "notification",
        "expected": {
            "customer": "COSCO Logistics Co., Ltd.",
            "product": "Ceramic floor tile 600x600mm",
            "quantity": "1,680 cartons",
            "price": "",
            "deadline": "2026-09-25"
        },
        "note": "通知类陷阱：含多个日期（ETD 2026-08-28 开船 / ETA 2026-09-25 到港），"
                "截止日应取**到港日 ETA**——测试日期仲裁能力"
    },
]


def main():
    with open(SRC, "r", encoding="utf-8") as f:
        emails = json.load(f)

    for m in emails:
        m["category"] = CAT.get(m["id"], "")

    emails.extend(EXTRA)

    with open(DST, "w", encoding="utf-8") as f:
        json.dump(emails, f, ensure_ascii=False, indent=2)

    print(f"[已生成] {DST}")
    print(f"样本总数: {len(emails)}")
    print("分类分布:", dict(Counter(m["category"] for m in emails)))


if __name__ == "__main__":
    main()
