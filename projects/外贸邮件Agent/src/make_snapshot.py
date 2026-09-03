# -*- coding: utf-8 -*-
"""
把 Agent Mail 收件箱里取到的原始邮件落成快照 JSON。

说明：
    邮件正文**原样保留**（含 HTML 标签、服务商页脚、退订链接与 token），
    不在此处做任何清洗 —— 清洗交给 mail_connector.clean_body()。
    这样才能验证清洗逻辑真的有效，而不是"数据本来就干净"。

    本脚本只是一次性搬运工具；正式流程应由 Agent Mail 连接器直接导出。
"""

import os
import json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "data", "inbox_snapshot.json")

# 以下是 2026-08-31 通过 Agent Mail 实际收发、再经 GetMessage 取回的原始内容
MAILS = [
    {
        "id": "M001",
        "message_id": "msg_f0adiPSAvW4OjT_QkK7KN_bMz7_r5U-jGoZXkE2xU45vEA",
        "from": "imwd4546@agent.qq.com",
        "to": "imwd4546@agent.qq.com",
        "subject": "Inquiry - Aluminum Composite Panel for Office Building Project",
        "created_at": "2026-08-31T02:11:43Z",
        "body": """<div style="white-space: pre-wrap; word-break: break-word;">Dear Sales Team,<br  /><br  />We are a general contractor based in Rotterdam and currently working on a 12-floor office building renovation project.<br  /><br  />We are interested in your aluminum composite panel (ACP), specification as below:<br  />- Panel size: 1220 x 2440 mm<br  />- Thickness: 4mm<br  />- Coating: PVDF, color to be confirmed<br  />- Estimated quantity: 8,500 sqm<br  /><br  />Could you please send us your best price per sqm on FOB Ningbo basis?<br  />Our target price is USD 14.20/sqm.<br  /><br  />We need the quotation before 2026-09-18 as we must submit the material budget to our client by end of September.<br  /><br  />Please also advise your production lead time and payment terms.<br  /><br  />Best regards,<br  />Marcus van Dijk<br  />Procurement Manager<br  />Van Dijk Bouwgroep B.V.<br  />Rotterdam, Netherlands</div>
<div data-xmail_bot_mail_report="true" style="margin: 24px 0; color: #999; font-size: 13px; line-height: 21px;">
  <div style="height: 0; overflow: hidden; border-top: 1px solid rgba(21, 46, 74, 0.07);"></div>
  <p style="margin: 0; padding: 12px 0; color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 23px;"><span style="line-height: 18px;">此邮件由</span><a href="https://agent.qq.com/page/identity?token=SnvcmE1Ew6q2R7ZfvgiaLjJCpew2i5LCv8qACv4-kLDqKXH_JdtXmJbPzKmVBTDndx1VDZBEg3FwRUeqGO7N7ZnWWe8zmhE80v1zn1cZHcd0pVEGhyrdbas" target="_blank" style="margin: 0 0 0 8px; color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 18px; text-decoration: none;">imwd4546@agent.qq.com</a><span style="color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 18px;">通过</span><a href="https://agent.qq.com" target="_blank" style="padding: 0 4px; color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 18px; text-decoration: none;">Agent Mail</a><span style="color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 18px;">自动发送。</span><span style="margin: 0 0 0 8px; color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 18px;"><a href="https://agent.qq.com/page/report?type=report&token=SnvcmE1Ew6q2R7ZfvgiaLjJCpew2i5LCv8qACv4-kLDqKXH_JdtXmJbPzKmVBTDndx1VDZBEg3FwRUeqGO7N7ZnWWe8zmhE80v1zn1cZHcd0pVEGhyrdbas" target="_blank" style="color: #0F52A0; font-size: 13px; line-height: 18px; padding: 0 8px; box-sizing: border-box; text-decoration: none;">举报</a><a href="https://agent.qq.com/page/report?type=unsubscribe&token=SnvcmE1Ew6q2R7ZfvgiaLjJCpew2i5LCv8qACv4-kLDqKXH_JdtXmJbPzKmVBTDndx1VDZBEg3FwRUeqGO7N7ZnWWe8zmhE80v1zn1cZHcd0pVEGhyrdbas" target="_blank" style="color: #0F52A0; font-size: 13px; line-height: 18px; padding: 0 8px; box-sizing: border-box; text-decoration: none;">退订</a></span></p>
</div>
""",
    },
    {
        "id": "M002",
        "message_id": "msg_v76IxfdRAa3HueqT6g0bCSivPSci83LGRn7i7XsC9eF9Hg",
        "from": "imwd4546@agent.qq.com",
        "to": "imwd4546@agent.qq.com",
        "subject": "Purchase Order PO-2026-0917 - LED Downlight 12W",
        "created_at": "2026-08-31T02:12:37Z",
        "body": """<div style="white-space: pre-wrap; word-break: break-word;">Hello,<br  /><br  />Further to your quotation Q-2608-114, we accept the price of USD 9.80/pc for the LED downlight 12W (Model DL-12W-RD, 3000K, round).<br  /><br  />Please proceed with production as per the details below:<br  /><br  />PO Number: PO-2026-0917<br  />Item: LED Downlight 12W, Model DL-12W-RD<br  />Quantity: 6,400 pcs<br  />Unit price: USD 9.80/pc FOB Ningbo<br  />Total amount: USD 62,720.00<br  /><br  />Required delivery date: 2026-10-20<br  />Shipping mark and packing details will follow.<br  /><br  />Please send us the proforma invoice for our signature.<br  /><br  />Kind regards,<br  />Aisha Rahman<br  />Purchase Executive<br  />Brightline Trading FZE<br  />Dubai, UAE</div>
<div data-xmail_bot_mail_report="true" style="margin: 24px 0; color: #999; font-size: 13px; line-height: 21px;">
  <div style="height: 0; overflow: hidden; border-top: 1px solid rgba(21, 46, 74, 0.07);"></div>
  <p style="margin: 0; padding: 12px 0; color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 23px;"><span style="line-height: 18px;">此邮件由</span><a href="https://agent.qq.com/page/identity?token=UYYWi9_9nb69zx3TiwDMMIeAEfz1NU4IMDLmrW-burPZ4poZpOkU8bQhNjqAK_U5CY21vyYFFhYOXRf9mKrqRwyPXML9LQSvqkAz6WeieKLK_wRuunI9HxE" target="_blank" style="margin: 0 0 0 8px; color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 18px; text-decoration: none;">imwd4546@agent.qq.com</a><span style="color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 18px;">通过</span><a href="https://agent.qq.com" target="_blank" style="padding: 0 4px; color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 18px; text-decoration: none;">Agent Mail</a><span style="color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 18px;">自动发送。</span><span style="margin: 0 0 0 8px; color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 18px;"><a href="https://agent.qq.com/page/report?type=report&token=UYYWi9_9nb69zx3TiwDMMIeAEfz1NU4IMDLmrW-burPZ4poZpOkU8bQhNjqAK_U5CY21vyYFFhYOXRf9mKrqRwyPXML9LQSvqkAz6WeieKLK_wRuunI9HxE" target="_blank" style="color: #0F52A0; font-size: 13px; line-height: 18px; padding: 0 8px; box-sizing: border-box; text-decoration: none;">举报</a><a href="https://agent.qq.com/page/report?type=unsubscribe&token=UYYWi9_9nb69zx3TiwDMMIeAEfz1NU4IMDLmrW-burPZ4poZpOkU8bQhNjqAK_U5CY21vyYFFhYOXRf9mKrqRwyPXML9LQSvqkAz6WeieKLK_wRuunI9HxE" target="_blank" style="color: #0F52A0; font-size: 13px; line-height: 18px; padding: 0 8px; box-sizing: border-box; text-decoration: none;">退订</a></span></p>
</div>
""",
    },
    {
        "id": "M003",
        "message_id": "msg__yIBP7uuveswy18DvN--q2qHQLNnKuC7yjPjqRVU3ZnxfQ",
        "from": "imwd4546@agent.qq.com",
        "to": "imwd4546@agent.qq.com",
        "subject": "URGENT Complaint - damaged ceramic floor tiles, Order PO-2026-0885",
        "created_at": "2026-08-31T02:13:37Z",
        "body": """<div style="white-space: pre-wrap; word-break: break-word;">Dear Sir,<br  /><br  />We received the ceramic floor tiles under PO-2026-0885 (3,200 sqm, 600x600mm matte finish) at our warehouse in Jebel Ali on 2026-08-26.<br  /><br  />Unfortunately, upon inspection we found that 260 sqm of the tiles were broken or chipped at the edges. The damage appears to be caused by insufficient protective packing between pallets. Photos are available on request.<br  /><br  />We request either:<br  />(a) free replacement of the damaged 260 sqm, shipped with our next order, or<br  />(b) a credit note of USD 2,400 for the damaged quantity.<br  /><br  />Please treat this as urgent because our site installation starts on 2026-09-15 and we are now short of material. We need your reply before 2026-09-08.<br  /><br  />Awaiting your prompt response.<br  /><br  />Omar Al-Farsi<br  />Project Manager<br  />Gulf Interiors LLC<br  />Dubai, UAE</div>
<div data-xmail_bot_mail_report="true" style="margin: 24px 0; color: #999; font-size: 13px; line-height: 21px;">
  <div style="height: 0; overflow: hidden; border-top: 1px solid rgba(21, 46, 74, 0.07);"></div>
  <p style="margin: 0; padding: 12px 0; color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 23px;"><span style="line-height: 18px;">此邮件由</span><a href="https://agent.qq.com/page/identity?token=gPQhw9P42bxY9ljHntxSKZ7YBbjE8dEpDAdo6O5hhu-Hb00jvjKTvtAv_so0YKiO2_KmQ7mrxk2MnHokSjliIh2PqEhO3HjhKaQqwVTj_72sbilmqu-Jq40" target="_blank" style="margin: 0 0 0 8px; color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 18px; text-decoration: none;">imwd4546@agent.qq.com</a><span style="color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 18px;">通过</span><a href="https://agent.qq.com" target="_blank" style="padding: 0 4px; color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 18px; text-decoration: none;">Agent Mail</a><span style="color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 18px;">自动发送。</span><span style="margin: 0 0 0 8px; color: rgba(24, 36, 48, 0.60); font-size: 13px; line-height: 18px;"><a href="https://agent.qq.com/page/report?type=report&token=gPQhw9P42bxY9ljHntxSKZ7YBbjE8dEpDAdo6O5hhu-Hb00jvjKTvtAv_so0YKiO2_KmQ7mrxk2MnHokSjliIh2PqEhO3HjhKaQqwVTj_72sbilmqu-Jq40" target="_blank" style="color: #0F52A0; font-size: 13px; line-height: 18px; padding: 0 8px; box-sizing: border-box; text-decoration: none;">举报</a><a href="https://agent.qq.com/page/report?type=unsubscribe&token=gPQhw9P42bxY9ljHntxSKZ7YBbjE8dEpDAdo6O5hhu-Hb00jvjKTvtAv_so0YKiO2_KmQ7mrxk2MnHokSjliIh2PqEhO3HjhKaQqwVTj_72sbilmqu-Jq40" target="_blank" style="color: #0F52A0; font-size: 13px; line-height: 18px; padding: 0 8px; box-sizing: border-box; text-decoration: none;">退订</a></span></p>
</div>
""",
    },
]


def main():
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(MAILS, f, ensure_ascii=False, indent=2)
    print(f"[已生成] {OUT}")
    print(f"邮件数: {len(MAILS)}")
    for m in MAILS:
        print(f"  {m['id']}  原始长度 {len(m['body'])} 字符  |  {m['subject'][:50]}")


if __name__ == "__main__":
    main()
