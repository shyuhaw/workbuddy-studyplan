# 外贸邮件 Agent · 每日收件箱处理（automation-1788145246236）

## 任务要点
- 拉 Agent Mail 收件箱**未读**邮件（dir=inbox, limit=20），无未读则直接结束、不生成文件。
- 有未读时：GetMessage 取正文 → 写 `data/inbox_snapshot.json` → 跑 `src/mail_connector.py`
  → 读 `output/真实邮件处理结果.xlsx` 汇总。
- 敏感内容（底价/合同条款/身份证号）跳过、不送 LLM。不改 `src/` 代码。

## 执行记录

### 2026-09-02 09:02（首次运行）
- **结果：无未读邮件，未生成任何文件。**
- 连接器状态：正常。`GetMe` 返回 scopes 含 `mail:read`，主别名 `imwd4546@agent.qq.com`
  （alias_GMPA6ETiJKrwq0d6JGxiG8UQIWE1XhsHGQ），限流 10 次/分钟。
- 收件箱共 4 封，全部 `is_read: true`，`has_more: false`（已穷尽，无分页遗漏）：
  1. URGENT Complaint - damaged ceramic floor tiles, Order PO-2026-0885（2026-08-31）
  2. Purchase Order PO-2026-0917 - LED Downlight 12W（2026-08-31）
  3. Inquiry - Aluminum Composite Panel for Office Building Project（2026-08-31）
  4. Agent Mail 接入成功（系统欢迎信，2026-08-31）
- 判定：这 4 封是 8/31 自建的演示邮件，上一轮已处理并标记已读；8/31 之后无新进邮件。

### 2026-09-03 09:02（第二次运行）
- **结果：无未读邮件，未生成任何文件。** 与首次运行完全一致。
- 收件箱仍为同样 4 封，全部 `is_read: true`，`has_more: false`，无新增。
- 连接器正常（无过滤复核调用成功返回数据）。
- **连续两次空跑**：若 2026-09-04 仍为空，建议向麦当确认该定时任务是否还要继续
  （演示邮箱无真实外部来信，任务长期空转）。

## 复用要点 / 踩坑
- **`is_read: false` 过滤返回空时，必须再用不带该过滤的 `ListMessages` 复核一遍**，
  以区分「真无未读」和「连接器/鉴权故障」——本次即靠复核确认为前者。
  空结果的标志是 `data` 里只有 `pagination` 和 `_hints`，没有 `data` 数组。
- 运行脚本须带两个环境变量：`MSYS_NO_PATHCONV=1`、`PYTHONIOENCODING=utf-8`。
- 本任务上限 10 次请求/分钟，GetMessage 逐封调用时注意别超。
- 上次产出参考：`output/真实邮件处理结果.xlsx`（2026-08-31 11:03）、
  `data/inbox_snapshot.json`（2026-08-31 11:03）。
