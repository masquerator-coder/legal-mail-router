# 排查：删除日志后邮件未被重新处理

## 现象

在 Web UI 的「处理日志」页面，手动删除了一条已处理的邮件记录。
删除后手动触发了调度器（仪表盘→立即检查），调度器从 IMAP 拉取了 26 封邮件并全部处理，
但被删除的那封邮件没有被重新处理（日志表中没有出现新的记录）。

邮件是昨天（2026-07-08）的新邮件，`monitor_days` 默认 7 天，不在日期窗口之外。

## 任务

请远程登录部署了 legal-mail-router 的服务器，**分步骤排查原因**：

### 步骤1：找到目标邮件

在 Docker 容器日志中，找到「已拉取」的邮件列表。用户说那封邮件在日志中可能叫"市容合同"或"合作协议"之类的标题。检查容器日志中 `已拉取: 市容合同` 或类似标题的记录是否出现重复（表明同一封被拉取了多次还是只拉取一次）。

运行：
```bash
docker logs legal-mail-router 2>&1 | grep -E "已拉取|跳过|发现|过滤"
```

### 步骤2：检查数据库中的去重状态

进入容器或挂载目录，查询 SQLite 数据库，确认被删除的邮件 `message_id` 是否确实不在 `email_logs` 表中：

```bash
sqlite3 data/legal_mail.db "SELECT id, subject, message_id, status, created_at FROM email_logs ORDER BY created_at DESC LIMIT 30;"
```

对比步骤1日志中的邮件主题，看是否有主题出现在「已拉取」日志中但**没有出现在数据库结果中**。

### 步骤3：检查是否因唯一约束冲突跳过

如果步骤2发现数据库中**有**该邮件的记录（说明删除没有真正生效），检查删除 API 是否正常工作：

```bash
# 查看最近的删除操作日志
docker logs legal-mail-router 2>&1 | grep -i "delete\|删除\|delete-selected\|clear"
```

### 步骤4：检查调度器处理过程中是否有静默错误

查看完整容器日志中是否有以下模式：
- `LLM 分析失败`
- `处理邮件 #N (标题) 失败`
- `TypeError`
- `IntegrityError`
- `UndefinedError`

```bash
docker logs legal-mail-router 2>&1 | grep -E "失败|error|Error|ERROR|TypeError|IntegrityError|Undefined|Exception|Traceback"
```

### 步骤5：检查关键代码逻辑

如果以上步骤没有发现明显问题，检查以下几点：

**A.** `_fetch_and_dedup()` 的去重查询是否可能因事务隔离看不到已提交的删除：
- 文件: `app/scheduler.py`, 第353-365行
- 创建独立的 `SessionLocal()` 查询 `EmailLog.message_id`
- 确认没有 `FOR UPDATE` 或 `READ UNCOMMITTED` 等锁定行为

**B.** 删除 API 的 `commit()` 是否真正写入了数据库：
- 文件: `app/routes/logs.py`, 第139-157行
- 检查 `db.commit()` 是否有可能因为 `OperationalError`(database is locked) 回滚后静默跳过

### 补充信息

- 项目位置: （用户指定）
- 技术栈: FastAPI + SQLAlchemy + SQLite, APScheduler, 运行在 Docker 容器中
- Python 版本 (宿主机): Python 3.14.5
- Python 版本 (容器内): Python 3.12-slim
- 容器名: legal-mail-router

## 输出要求

- 给出每一步的执行结果（有输出贴输出，无输出注明）
- 最终给出根因判断
