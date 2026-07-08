# 文书分拣系统 (Legal Mail Router)

自动监控邮箱、AI 分析法律文书、智能转发到对应律师的邮件分拣系统。

## 功能特性

- **多邮箱监控** — 支持 163、Gmail、QQ 等主流邮箱，定时拉取新邮件
- **163.com 深度兼容** — 原始 SSL Socket + 缓冲 I/O，绕过 Python imaplib 限制
- **IMAP 特殊字符安全** — RFC 3501 ASTRING 转义，支持含 `"` 和 `\` 的授权码
- **AI 智能分析** — 调用 LLM（OpenAI 兼容 API）识别文书类型、提取案号、关键日期、风险评估
- **多模态图片输入** — 支持图片附件直接送入多模态 LLM 分析
- **OCR 图片识别** — 支持图片附件文字识别（PaddleOCR / OpenAI Vision）
- **PDF 智能提取** — 文字型 PDF 直接提取，扫描件 PDF 自动 OCR + 多模态降级
- **附件文本提取** — 支持 PDF、DOCX、DOC（antiword）、XLSX、TXT 等格式
- **灵活路由规则** — 一条规则可配置多个接收邮箱（逗号分隔）；账户专属 → 全局兜底；LLM 类型 + 关键词双路径
- **法律知识库集成** — 接入 LLM Wiki 知识库，分析邮件时自动检索相关法律法规参考
- **关键词兜底** — 无 LLM 配置时仍可通过关键词匹配路由转发
- **多律师分发** — 按文书类型自动转发，附带 AI 分析摘要，一条规则对应一个转发目标
- **发件人过滤** — 可按发件人白名单/黑名单控制邮件处理范围
- **重复邮件防护** — 基于 Message-ID 的批量查重
- **SMTP 自动推断** — 从 IMAP 域名自动推断同域 SMTP 配置（10 个服务商）
- **智能垃圾过滤** — LLM 置信度 <30% 的非法律文书自动跳过
- **修改版文书生成** — AI 分析完成后自动生成修订版文书，改动用颜色标注
- **文书模板管理** — 按文书类型 + 邮箱账户配置格式模板，同类型同账户唯一
- **上下文窗口探测** — 内置 43 个模型窗口表，支持一键自动探测
- **审核意见模板** — 使用 .docx 模板生成审核意见书，自动替换 xxx 占位符
- **三种 .docx 输出** — AI 分析报告、修改版文书、审核意见书
- **LLM 提示词自定义** — 可自定义分析提示词、修订提示词
- **Web 管理后台** — 可视化配置邮箱、LLM、路由、模板、OCR、系统参数
- **实时进度跟踪** — Web 轮询显示处理进度
- **配置备份恢复** — 一键导出 / 导入全量配置（含多账户路由）
- **每日运行报告** — 定时邮件汇总报告（含零除保护）
- **服务在线重启** — Web 界面一键热重启（Docker 环境下自动适配端口）
- **数据库自动迁移** — 升级时自动补齐新增列、迁移字段格式
- **无界面独立运行** — 调度器后台自动运行

### 安全性

- **CSRF 保护** — 所有状态变更操作（增删改）均通过 POST + CSRF Token 校验，无 GET 裸操作
- **加密改进** — Fernet 纯随机密钥生成，旧 PBKDF2 格式自动迁移，同机同网不再可预测
- **HTTPS 可选** — 设置 `FORCE_HTTPS=true` 环境变量即可启用 secure cookie
- **管理员用户名可配置** — `admin_username` 数据库设置项，不再硬编码 `admin`
- **密码哈希** — PBKDF2-SHA256 (100,000 次迭代)
- **登录频率限制** — 15 分钟内最多 5 次失败尝试
- **Session 持久化密钥** — 服务重启不失效

## 技术栈

| 组件 | 技术 |
|------|------|
| Web 框架 | FastAPI + Uvicorn |
| 数据库 | SQLite (WAL 模式，busy_timeout=30s，自动迁移) |
| 模板引擎 | Jinja2 |
| 任务调度 | APScheduler（BackgroundScheduler，Asia/Shanghai） |
| LLM 调用 | httpx (OpenAI 兼容 API)，支持多模态 |
| 文档解析 | PyMuPDF / python-docx / openpyxl / antiword |
| Word 生成 | python-docx（着色标注、格式保留、段落填充） |
| 加密 | cryptography (Fernet) |
| 认证 | Session + CSRF，持久化密钥，登录频率限制 |

## 快速开始

```bash
git clone <repo-url> legal-mail-router
cd legal-mail-router

python3 -m venv venv
source venv/bin/activate
pip install -e .

# 环境检查
python run.py check

# 前台启动
python run.py

# 后台启动
python run.py start --daemon
./start.sh start
```

访问 `http://localhost:8020`，首次启动自动生成管理员密码（查看终端日志）。

### 管理命令

```
python run.py {start|stop|restart|status|install|check|test|db-init}
./start.sh {start|stop|restart|status|logs|install|test}
```

## Docker 部署

```bash
docker build -t legal-mail-router .

docker run -d \
  --name legal-mail-router \
  --restart=unless-stopped \
  -p 8020:8020 \
  -v $(pwd)/data:/app/data \
  -e TZ=Asia/Shanghai \
  -e FORCE_HTTPS=false \
  legal-mail-router

docker compose up -d
```

查看管理员密码：`docker logs legal-mail-router | grep 初始管理员密码`

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `8020` | 服务端口 |
| `TZ` | — | 时区（建议 `Asia/Shanghai`） |
| `FORCE_HTTPS` | `false` | 设为 `true` 启用 secure cookie（公网部署必须） |
| `DOCKER_CONTAINER` | — | Docker 自动检测，端口由 env 控制 |

### 数据持久化

| 文件 | 说明 |
|------|------|
| `data/legal_mail.db` | 所有配置和处理记录 |
| `data/.encryption_key` | 加密密钥（纯随机生成），丢失无法解密密码 |
| `data/.session_secret` | Web 会话密钥，重启不失效 |
| `data/attachments/` | 邮件附件 |

> 跨机器迁移时须同时复制 `.encryption_key` 和 `.session_secret`。

## 首次配置

1. **系统设置** — 系统名称、监控天数、上下文窗口、输出方式等
2. **LLM 配置** — OpenAI 兼容 API 地址和 Key（支持 text / multimodal）
3. **邮箱配置** — 添加监控邮箱（IMAP + 授权码），可配置发件人过滤
4. **路由规则** — 一条规则可选择多个接收邮箱（逗号分隔），配置文书类型→律师邮箱的转发
5. **文书模板**（可选） — 为各文书类型配置格式模板（支持按邮箱区分）
6. **OCR 配置**（可选） — 图片附件文字识别
7. **法律知识库**（可选） — 接入 LLM Wiki 知识库，AI 分析时注入相关法规

## 路由规则

### 规则结构

每条规则包含：
- **接收邮箱**（可多选） — 哪些邮箱收到的邮件适用本规则；不选 = 全局规则
- **文书类型** — 匹配的文书类型（如：起诉状、判决书、合同协议）
- **关键词**（可选） — 逗号分隔的匹配词，在邮件正文/附件中搜索
- **转发目标** — 单一律师邮箱 + 姓名
- **专用 SMTP**（可选） — 覆盖默认 SMTP 配置

### 匹配优先级

1. LLM 文书类型精确匹配
2. LLM 律师类型匹配
3. 模糊子串匹配
4. 关键词匹配（有无 LLM 均生效）

账户专属规则优先于全局规则，同层按优先级（数字越大越优先）排序。

## 输出方式

| 类型 | 说明 |
|------|------|
| AI 分析报告 | LLM 结构化分析报告 |
| 修改版文书 | 按审核意见修订的文书，蓝色新增 / 红色修改 / 删除线建议删除 |
| 审核意见书 | 按 .docx 模板填充，仅替换 xxx 占位符 |

## 目录结构

```
legal-mail-router/
├── app/
│   ├── main.py              # FastAPI 入口 + 中间件链
│   ├── config.py            # 配置 + Fernet 加密（纯随机密钥）
│   ├── database.py          # 数据库引擎 + 自动迁移 + 重试提交
│   ├── models.py            # 数据模型（EmailAccount/LLMConfig/RoutingRule/DocTemplate 等）
│   ├── auth.py              # 认证 + 可配置用户名 + 登录频率限制
│   ├── csrf.py              # CSRF 保护（全 POST 覆盖）
│   ├── email_fetcher.py     # 邮件拉取（163 raw socket / 标准 IMAP）+ 附件提取
│   ├── kb_client.py         # 法律知识库 HTTP 客户端（Hybrid 检索）
│   ├── llm_analyzer.py      # LLM 分析 + 修订生成 + 上下文窗口探测
│   ├── mail_forwarder.py    # SMTP 转发 + Word 生成 + 模板填充
│   ├── ocr.py               # OCR 识别 + 模型类型检测 + 响应异常处理
│   ├── scheduler.py         # 定时调度 + 进度追踪 + 路由匹配 + 每日报告 + 附件清理
│   └── routes/
│       ├── dashboard.py     # 仪表盘
│       ├── email_config.py  # 邮箱管理
│       ├── llm_config.py    # LLM 配置
│       ├── ocr_config.py    # OCR 配置
│       ├── routing.py       # 路由规则（多账户支持）
│       ├── doc_templates.py # 文书模板 CRUD（按账户区分）
│       ├── logs.py          # 处理日志
│       ├── settings.py      # 系统设置 + 在线重启
│       └── backup.py        # 配置备份/恢复（多账户路由兼容）
├── templates/               # Jinja2 模板（11 个页面）
├── static/                  # 静态资源
├── tests/                   # 测试套件
├── run.py                   # Python 启动脚本
├── start.sh                 # Shell 启动脚本
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

## Troubleshooting

### 邮件卡在"待处理"状态无法处理

系统异常崩溃（如 `UnboundLocalError`）时，已拉取到数据库的邮件会卡在 `pending` 状态，后续调度因查重跳过导致永不处理。

**修复**：删除该记录，下次调度会重新拉取。

```bash
# 1. 查出待处理邮件的 ID
docker exec legal-mail-router python -c "
from app.database import SessionLocal;
from app.models import EmailLog;
db=SessionLocal();
ids=[r.id for r in db.query(EmailLog).filter(EmailLog.status == 'pending').all()];
print(f'待处理邮件ID: {ids}');
db.close()
"

# 2. 删除这些记录（含附件）
docker exec legal-mail-router python -c "
from app.database import SessionLocal;
from app.models import EmailLog, Attachment;
db=SessionLocal();
ids=[6,8,9,12,14,16,17,18];  # ← 替换为第1步查出的ID
db.query(Attachment).filter(Attachment.log_id.in_(ids)).delete(synchronize_session=False);
c=db.query(EmailLog).filter(EmailLog.id.in_(ids)).delete(synchronize_session=False);
db.commit(); db.close();
print(f'已删除 {c} 条待处理记录')
"
```

## License

MIT
