# 文书分拣系统 (Legal Mail Router) v1.0

自动监控邮箱、AI 分析法律文书、智能转发到对应处理人的邮件分拣系统。

## 功能特性

### 邮件处理
- **多邮箱监控** — 支持 163、Gmail、QQ 等主流邮箱，定时拉取新邮件
- **163.com 深度兼容** — 原始 SSL Socket + 缓冲 I/O，绕过 Python imaplib 对 163 的限制
- **IMAP 特殊字符安全** — RFC 3501 ASTRING 转义，支持含 `"` 和 `\` 的授权码
- **发件人过滤 + 全局黑名单** — 每个邮箱账户可配置发件人白名单/黑名单，同时支持全局发件人黑名单（两者 OR 组合，任一匹配即过滤）
- **重复邮件防护** — 基于 Message-ID 的批量查重，同一封邮件不会被重复处理
- **附件文本提取** — 支持 PDF、DOCX、DOC（antiword）、XLSX、TXT 等格式

### AI 智能分析
- **LLM 分析** — 调用 OpenAI 兼容 API 识别文书类型、提取案号、关键日期、风险评估、涉及方
- **多模态图片输入** — 图片附件直接送入多模态 LLM 分析，支持多张图片
- **OCR 识别 + PDF 能力检测** — 支持 PaddleOCR / OpenAI Vision / 自定义 OCR 服务；两步检测机制（先测连通性，再测 PDF 直读能力）；文字型 PDF 直接提取，扫描件 PDF 自动 OCR + 多模态降级
- **附件预分类（多文书分组分析）** — LLM 先按文书类型对附件分组，然后逐组独立分析和修订
- **法律知识库集成** — 接入 LLM Wiki 知识库，分析邮件时自动检索相关法律法规参考
- **智能垃圾过滤** — LLM 置信度 <30% 且判定为「非法律文书」或「其他法律文书」的邮件自动跳过，不转发

### 路由与转发
- **简化路由规则** — 每个转发规则仅包含：
  - `account_ids`：逗号分隔的邮箱账户 ID（空 = 全局规则）
  - `target_email`：逗号分隔的转发目标邮箱
  - 不再按文书类型/关键词匹配，不再支持每条规则独立 SMTP
- **转发决策树**（由 `_get_forward_targets()` 自动执行）：
  1. **垃圾过滤**：置信度 <0.3 且非法律文书 → 跳过，不转发
  2. **非法律文书**：置信度 ≥0.3 → 转发到默认邮箱
  3. **法律文书**：按 `account_ids` 匹配路由规则 → 并发转发到所有命中目标
  4. **兜底**：法律文书无规则匹配 → 转发到默认邮箱
- **SMTP 自动推断** — 从 IMAP 域名自动推断同域 SMTP 配置（支持 10 个以上服务商）
- **转发失败挽救** — SMTP 发送失败时自动重试，Web 管理后台提供手工重发按钮

### 文档输出
- **AI 分析报告** — LLM 结构化 JSON 分析结果，存入数据库可按处理日志查阅
- **修改版文书** — 仅对主要文书类型（起诉状、判决书、合同协议等）生成修改版 Word 文档，改动处颜色标注：**蓝色 = 新增**、**红色 = 修改**、**红色删除线 = 建议删除**
- **审核意见书** — 使用 .docx 模板填入审核意见，仅替换模板中的 `xxx` 占位符

### 提示词自定义
- **LLM 提示词自定义** — `LLM提示词.md` 独立文件，可自由修改分析提示词
- **修订提示词自定义** — `修订提示词.md` 独立文件，可自定义文书修订生成提示词

### Token 预算管理
- **上下文窗口探测** — 内置 43+ 个已知模型的窗口大小表，支持一键自动探测模型实际窗口
- **优先级截断** — 按「法律知识库→附件文本（按附件边界）→邮件正文（从尾部）→提示词模板（不裁剪）」的优先级截断
- **两种估算方法** — 精确估算（tiktoken）和近似估算（基于字符数），自动选择

### Web 管理后台
- **全套 Web 管理** — 可视化配置邮箱、LLM、路由规则、OCR、文书模板、系统参数
- **实时进度跟踪** — Web 轮询实时显示邮件处理进度
- **配置备份恢复** — 一键导出/导入全量配置（含多账户路由）
- **每日运行报告** — 定时邮件汇总当日处理情况（含零除保护）
- **服务在线重启 + 自动更新** — Web UI 一键重启服务；支持 git 自动拉取远程更新并在 Web 界面一键应用
- **处理日志发件人列** — 处理日志表中增加发件人地址列，方便追溯

### 安全性
- **CSRF 保护** — 所有状态变更操作（增删改）均通过 POST + CSRF Token 校验，无 GET 裸操作
- **加密改进** — Fernet 纯随机密钥生成，旧 PBKDF2 格式自动迁移
- **HTTPS 可选** — 设置 `FORCE_HTTPS=true` 环境变量即可启用 secure cookie
- **管理员用户名可配置** — 支持自定义 `admin_username`，不再硬编码 `admin`
- **密码哈希** — PBKDF2-SHA256 (100,000 次迭代)
- **登录频率限制** — 15 分钟内最多 5 次失败尝试
- **Session 持久化密钥** — 服务重启不失效

### 运维
- **数据库自动迁移** — 升级时自动补齐新增列、迁移字段格式
- **版本管理** — `app/config.py` 中 `VERSION` 常量统一管理版本
- **无界面独立运行** — 调度器后台自动运行，无需常驻 Web 页面

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
| OCR | PaddleOCR / OpenAI Vision / 自定义 HTTP 服务 |
| 加密 | cryptography (Fernet) |
| 认证 | Session + CSRF，持久化密钥，登录频率限制 |

## 快速开始

```bash
git clone <repo-url> legal-mail-router
cd legal-mail-router

python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
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

```bash
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
4. **路由规则** — 添加转发规则（选择邮箱账户 → 填写转发目标邮箱）
5. **OCR 配置**（可选） — 图片附件文字识别，系统自动检测 PDF 直读能力
6. **文书模板**（可选） — 按文书类型 + 邮箱账户配置格式模板
7. **法律知识库**（可选） — 接入 LLM Wiki 知识库，AI 分析时注入相关法规

## 转发决策流程

```
收到邮件 → LLM 分析
├─ 置信度 < 0.3 且非法律文书 → 跳过（垃圾过滤）
├─ 非法律文书（置信度 ≥ 0.3） → 转发到默认邮箱
└─ 法律文书 → 路由规则匹配（按 account_ids）
   ├─ 命中规则 → 并发转发到所有命中目标
   └─ 无规则匹配 → 兜底转发到默认邮箱
```

路由规则字段：`account_ids`（关联邮箱账户 ID，逗号分隔，空=全局规则）、`target_email`（转发目标邮箱，逗号分隔支持多个目标）。
不再使用文书类型匹配、关键词匹配和每条规则独立 SMTP 配置。

## 目录结构

```
legal-mail-router/
├── app/
│   ├── main.py              # FastAPI 入口 + 中间件链
│   ├── config.py            # 配置 + Fernet 加密（VERSION = v1.0）
│   ├── database.py          # 数据库引擎 + 自动迁移 + 重试提交
│   ├── models.py            # 数据模型（EmailAccount/LLMConfig/RoutingRule/DocTemplate 等）
│   ├── auth.py              # 认证 + 可配置用户名 + 登录频率限制
│   ├── csrf.py              # CSRF 保护（全 POST 覆盖）
│   ├── email_fetcher.py     # 邮件拉取（163 raw socket / 标准 IMAP）+ 附件提取 + 黑名单过滤
│   ├── kb_client.py         # 法律知识库 HTTP 客户端（Hybrid 检索）
│   ├── llm_analyzer.py      # LLM 分析 + 修订生成 + 上下文窗口探测 + 多文书分组
│   ├── mail_forwarder.py    # SMTP 转发 + Word 生成 + 模板填充 + 重试机制
│   ├── ocr.py               # OCR 识别 + 模型类型检测 + 响应异常处理 + PDF 直读
│   ├── ocr_test_data.py     # 预制测试 PDF/PNG 数据，用于验证 OCR 配置
│   ├── prompt_budget.py     # Token 预算管理：估算、优先级截断、上下文窗口对齐
│   ├── scheduler.py         # 定时调度 + 转发决策 + 进度追踪 + 每日报告
│   ├── flash.py             # Flash 消息中间件
│   ├── __init__.py
│   └── routes/
│       ├── __init__.py      # 路由注册（集中导入所有子路由）
│       ├── dashboard.py     # 仪表盘
│       ├── email_config.py  # 邮箱管理
│       ├── llm_config.py    # LLM 配置
│       ├── ocr_config.py    # OCR 配置 + 连通性/PDF 能力检测
│       ├── routing.py       # 路由规则管理
│       ├── doc_templates.py # 文书模板 CRUD
│       ├── logs.py          # 处理日志（含发件人地址列）
│       ├── settings.py      # 系统设置 + 在线重启
│       ├── backup.py        # 配置备份/恢复
│       └── auto_update.py   # 自动更新：git 拉取 + Web UI 一键应用
├── config/
│   ├── __init__.py
│   └── model_windows.py     # 43+ 个已知 LLM 模型的上下文窗口表
├── templates/               # Jinja2 模板
├── static/                  # 静态资源
├── tests/                   # 测试套件
├── scripts/
│   └── generate_test_pdf.py # 生成预制测试 PDF 文件
├── LLM提示词.md             # AI 分析提示词（可自定义）
├── 修订提示词.md             # 文书修订生成提示词（可自定义）
├── run.py                   # Python 启动脚本
├── start.sh                 # Shell 启动脚本
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

## Troubleshooting

### 邮件卡在「待处理」状态无法处理

系统异常崩溃时，已拉取到数据库的邮件会卡在 `pending` 状态，后续调度因查重跳过导致永不处理。

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
