# 邮件智能分析转发系统 (Legal Mail Router) v2.0.0

自动监控邮箱、AI 分析法律文书、智能转发到对应处理人的邮件分拣系统，面向律师事务所的收件审核场景。

## 核心特性

- **多邮箱监控** — 支持 163 / Gmail / QQ / 标准 IMAP，定时拉取新邮件
- **AI 全流程分析** — 附件分组 → 文书类型识别 → 按类型专属审核，输出结构化分析报告
- **智能路由转发** — 按邮箱账户匹配转发规则，并发送达目标处理人，失败自动重试
- **文档成果输出** — 分析报告入库、修改版 Word 文书（改动着色标注）、审核意见书
- **全套 Web 管理后台** — 邮箱 / LLM / 路由 / OCR / 系统设置可视化配置，实时进度跟踪

---

## 目录

1. [核心特性](#核心特性)
2. [处理流程总览](#处理流程总览)
3. [功能特性](#功能特性)
4. [技术栈](#技术栈)
5. [快速开始](#快速开始)
6. [Docker 部署](#docker-部署)
7. [环境变量](#环境变量)
8. [数据持久化](#数据持久化)
9. [首次配置](#首次配置)
10. [转发决策流程](#转发决策流程)
11. [提示词自定义体系](#提示词自定义体系)
12. [目录结构](#目录结构)
13. [Troubleshooting](#troubleshooting)
14. [License](#license)

## 处理流程总览

```
收到邮件
  │
  ▼
① 拉取与预处理    附件提取（PDF/DOCX/DOC/XLSX/TXT/图片/压缩包）→ 保存原始附件
  │                   压缩包自动解压（zip/rar/7z/tar 等，仅用于分析链路）
  │
  ▼
② 附件预分类      LLM 按文书类型对附件分组（可关闭；同组附件视为同一份文书）
  │
  ▼
③ 类型识别        LLM 判定文书类型（文书类型.conf 候选清单）
  │                   非法律文书 → 跳过分析，按置信度归档或转发
  ▼
④ 类型专属分析    嵌入 分析提示词/<类型>.md → LLM 按类型专属流程审核
  │                   输出：案件摘要 / 风险分级 / 关键日期 / 案号 / 涉及方 / 修改版文书
  ▼
⑤ 路由转发        转发决策树 → 命中规则并发转发 / 兜底默认邮箱
```

每次分析调用对应**一份文书**（可能是多个附件组成的一组），类型在识别阶段确定后不再重复判断。

## 功能特性

### 邮件处理

- **多邮箱监控** — 支持 163、Gmail、QQ 等主流邮箱（`provider_type`：auto/163/gmail/qq/standard），定时拉取新邮件
- **163.com 深度兼容** — 原始 SSL Socket + 缓冲 I/O，绕过 Python imaplib 对 163 的限制
- **IMAP 特殊字符安全** — RFC 3501 ASTRING 转义，支持含 `"` 和 `\` 的授权码
- **发件人过滤 + 全局黑名单** — 每个邮箱账户可配置发件人白名单/黑名单，同时支持全局发件人黑名单（两者 OR 组合，任一匹配即过滤）
- **重复邮件防护** — 基于 Message-ID 的批量查重，同一封邮件不会被重复处理
- **附件文本提取** — 支持 PDF、DOCX、DOC（antiword）、XLSX、TXT 等格式
- **压缩包附件解压** — 附件为 zip / tar / tar.gz / gz / bz2 / xz（内置支持）或 rar / 7z（需系统安装 7z/unrar/unar，Docker 镜像已内置 p7zip-full 与 unar，rar 含 RAR5）时自动解压；解压后的文件**仅用于分组与分析链路**，磁盘与转发附件**保留原始压缩包**，与原邮件附件一致；内置 zip-slip 路径穿越防护、解压大小/数量/嵌套深度限制；解压失败时保留原附件不中断处理
- **附件路径沙箱** — 附件读取/删除限定在 `data/` 目录与系统临时目录内，防止路径越界

### AI 智能分析

- **多阶段 LLM 分析** — 附件预分类分组 → 第一阶段类型识别 → 第二阶段按类型嵌入专属分析流程提示词
- **多模态图片输入** — 图片附件直接送入多模态 LLM 分析，支持多张图片
- **OCR 识别 + PDF 能力检测** — 支持 PaddleOCR / OpenAI Vision / 自定义 OCR 服务；两步检测机制（先测连通性，再测 PDF 直读能力）；文字型 PDF 直接提取，扫描件 PDF 自动 OCR + 多模态降级
- **附件预分类（多文书分组分析）** — LLM 先按文书类型对附件分组（如合同正文+签章页归为一组），然后逐组独立执行识别与分析和修订
- **MCP 工具集成（法律检索）** — 提供 MCP（Model Context Protocol）连接，第二阶段文书分析时 LLM 可通过工具实时查询「北大法宝」等权威法规数据库（如 `adjust_provisions` 取权威条文原文），以检索结果为准引用、严禁凭记忆编造法条，并在报告/修改版文书末尾附来源链接；工具不可用时自动降级
- **智能垃圾过滤** — LLM 置信度 < 30% 且判定为「非法律文书」或「其他法律文书」的邮件自动跳过，不转发
- **模型角色分配** — LLM 配置页集中分配三个角色：分组模型、类型识别模型、文书解读审核模型；未指定时自动回退（类型识别/分组 → 文书解读，文书解读 → 第一个激活配置）

### 路由与转发

- **简化路由规则** — 每个转发规则仅包含：
  - `account_ids`：逗号分隔的邮箱账户 ID（空 = 全局规则）
  - `target_email`：逗号分隔的转发目标邮箱
- **SMTP 自动推断** — 从 IMAP 域名自动推断同域 SMTP 配置（支持 10 个以上服务商）
- **发件服务器优先级** — 转发优先用**收件邮箱自身的 SMTP** 发送（发件人即监控邮箱），不可用时自动回退系统默认 SMTP
- **转发失败挽救** — SMTP 发送失败时自动重试，并在主发件服务器不可用时自动切换备用发件服务器；Web 管理后台提供手工重发按钮

### 文档输出

- **AI 分析报告** — LLM 结构化 JSON 分析结果存入数据库，可在处理日志页查阅
- **修改版文书** — 仅对主要文书类型（起诉状、判决书、合同协议等）生成修改版 Word 文档，改动处颜色标注：**蓝色 = 新增**、**红色 = 修改**、**红色删除线 = 建议删除**
- **审核意见书** — 使用 `.docx` 模板填入审核意见，仅替换模板中的 `xxx` 占位符

### Token 预算管理

- **上下文窗口探测** — 内置 43+ 个已知模型的窗口大小表，支持一键自动探测模型实际窗口
- **优先级截断** — 按「附件文本（按附件边界）→ 邮件正文（从尾部）→ 提示词模板（不裁剪）」的优先级截断
- **两种估算方法** — 精确估算（tiktoken）和近似估算（基于字符数），自动选择

### Web 管理后台

- **全套 Web 管理** — 可视化配置邮箱、LLM、路由规则、OCR、系统参数
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
- **密码哈希** — PBKDF2-SHA256（100,000 次迭代）
- **登录频率限制** — 15 分钟内最多 5 次失败尝试
- **Session 持久化密钥** — 服务重启不失效

### 运维

- **数据库自动迁移** — 升级时自动补齐新增列、迁移字段格式
- **版本管理** — `app/settings.py` 中 `VERSION` 常量统一管理版本
- **无界面独立运行** — 调度器后台自动运行，无需常驻 Web 页面
- **日志保留策略** — 可配置处理日志保留天数（默认永久保留）

## 技术栈

| 组件 | 技术 |
|------|------|
| Web 框架 | FastAPI + Uvicorn |
| 数据库 | SQLite (WAL 模式，busy_timeout=30s，自动迁移) |
| 模板引擎 | Jinja2 |
| 任务调度 | APScheduler（BackgroundScheduler，Asia/Shanghai） |
| LLM 调用 | httpx (OpenAI 兼容 API)，支持多模态 |
| 文档解析 | PyMuPDF / python-docx / openpyxl / antiword |
| 压缩包解压 | zipfile / tarfile / gzip / bz2 / lzma（内置），rar/7z 走系统 7z/unrar/unar 命令 |
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

| 命令 | 说明 |
|------|------|
| `python run.py` | 前台启动（默认端口 8020，可用 `--port` 覆盖） |
| `python run.py start --daemon` | 后台守护进程启动（PID 写入 `log/legal-mail.pid`） |
| `python run.py stop / restart / status` | 停止 / 重启 / 查看状态 |
| `python run.py install` | 安装/更新依赖 |
| `python run.py check` | 环境检查（依赖、数据库、antiword） |
| `python run.py test` | 运行测试套件 |
| `python run.py db-init` | 仅初始化数据库 |

> 端口解析顺序：CLI `--port` > 数据库 `system_port` 设置 > 默认 8020。

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

# 或使用 docker compose
docker compose up -d
```

查看管理员密码：`docker logs legal-mail-router | grep 初始管理员密码`

> 容器以非 root 用户（uid 10001）运行；挂载的 `data/` 目录需可写：`chown -R 10001:10001 data/`。
> 镜像默认使用国内清华源（APT/PIP），可通过 `--build-arg APT_MIRROR / PIP_INDEX_URL` 覆盖。

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PORT` | `8020` | 服务端口（docker-compose 端口映射跟随该变量） |
| `TZ` | — | 时区（建议 `Asia/Shanghai`） |
| `FORCE_HTTPS` | `false` | 设为 `true` 启用 secure cookie（公网部署必须） |
| `DOCKER_CONTAINER` | — | Docker 自动检测，端口由 env 控制 |

## 数据持久化

| 文件 | 说明 |
|------|------|
| `data/legal_mail.db` | 所有配置和处理记录 |
| `data/.encryption_key` | 加密密钥（纯随机生成），丢失无法解密密码 |
| `data/.session_secret` | Web 会话密钥，重启不失效 |
| `data/attachments/` | 邮件附件（压缩包按原邮件原样保存；解压文件不落盘，仅用于分析） |

> 跨机器迁移时须同时复制 `.encryption_key` 和 `.session_secret`。

## 首次配置

1. **系统设置** — 系统名称、监控天数、上下文窗口、输出方式、修改版文书开关等
2. **LLM 配置** — OpenAI 兼容 API 地址和 Key（支持 text / multimodal），并分配三个模型角色（分组 / 类型识别 / 文书解读）
3. **邮箱配置** — 添加监控邮箱（IMAP + 授权码），可配置发件人过滤
4. **路由规则** — 添加转发规则（选择邮箱账户 → 填写转发目标邮箱）
5. **OCR 配置**（可选） — 图片附件文字识别，系统自动检测 PDF 直读能力

### MCP 工具（法律检索，可选）
6. **系统设置 → 🔗 MCP 工具（法律检索）** — 启用；配置已预填北大法宝 9 个服务器，只需把其中的 Token 占位符 `__PKULAW_TOKEN__` 替换为你的真实 Token 并保存。点击「列出可用工具」验证连接。启用后，LLM 在第二阶段文书分析时可调用这些工具查询权威法规，依据返回的条文原文引用，并在报告/修改版文书末尾自动附来源链接。MCP 连接失败时自动降级为普通分析，不影响邮件流转。

## 转发决策流程

```
收到邮件 → 第一阶段：文书类型识别
├─ 判定为「非法律文书」 → 直接归档/转发（跳过分析）
└─ 判定为法律文书 → 第二阶段：按类型嵌入分析提示词 → LLM 详细分析
   ├─ 置信度 < 0.3 且非法律文书 → 跳过（垃圾过滤）
   ├─ 非法律文书（置信度 ≥ 0.3） → 转发到默认邮箱
   └─ 法律文书 → 路由规则匹配（按 account_ids）
      ├─ 命中规则 → 并发转发到所有命中目标
      └─ 无规则匹配 → 兜底转发到默认邮箱
```

路由规则字段：`account_ids`（关联邮箱账户 ID，逗号分隔，空=全局规则）、`target_email`（转发目标邮箱，逗号分隔支持多个目标）。
不再使用文书类型匹配、关键词匹配和每条规则独立 SMTP 配置（`doc_type`/`keywords`/`priority` 字段为历史遗留，当前版本未启用；`smtp_*` 字段已移除）。

## 提示词自定义体系

系统采用**文件驱动**的提示词管理，无需改代码即可调整 AI 行为：

| 文件 | 作用 |
|------|------|
| `LLM提示词模板.md` | 主模板：角色定位、处理流程说明、步骤一（嵌入类型专属流程）、步骤二（生成审核报告）、步骤三（生成修改版文书） |
| `文书类型.conf` | 文书类型候选清单，每行一个类型；新增类型在此追加 |
| `分析提示词/<类型>.md` | 每种文书独立的专属分析流程（合同协议、起诉状、判决书、裁定书、传票、律师函、证据材料、通知书、其他法律文书） |

### 主模板结构

- **处理流程说明** — 告知 LLM 类型已由识别阶段确定、本次输入=一份文书（可能含多个附件）、非法律文书兜底
- **步骤一：按文书类型专属流程审核** — 嵌入 `{analysis_instructions}` 占位符（按类型自动替换为 `分析提示词/<类型>.md` 内容）；专属文件缺失时回退「其他法律文书」提示词，仍为空则走通用五维度兜底说明
- **步骤二：生成审核报告** — 对应输出字段 `ai_interpretation`，须覆盖案情摘要、法律要点、关键信息、风险分级（日期以 `{today}` 基准显式计算）、处理建议
- **步骤三：生成修改版文书** — 修订规则、改动标记规范（`【新增】`/`【修改】`/`【删除】`）、输出规则

### 分类型提示词约定

- 每个类型文件末尾的「修订版文书要求」决定该类型的修订策略：**必须生成**（合同/协议）/ **可修订**（起诉状、律师函等）/ **通常无需修订**（裁判、通知、证据类，`revised_document` 为 null）
- 引用主模板修订规则时使用**稳定章节名**「生成修改版文书」，不使用步骤编号（避免编号变动导致引用失效）

### 自定义注意事项

- 主模板支持占位符：`{subject}` `{sender}` `{body}` `{today}` `{analysis_instructions}`；分类型提示词支持 `{today}`
- LLM 配置页可分别覆盖三个阶段的提示词（分组模板 / 类型识别模板 / 分析主模板）；自定义模板未含 `{analysis_instructions}` 占位符时，类型专属流程会自动追加到末尾
- 提示词模板在 Token 预算截断中**永不裁剪**，请保持精简

## 目录结构

```
legal-mail-router/
├── app/
│   ├── main.py              # FastAPI 入口 + 中间件链
│   ├── settings.py          # 路径定义 + 全局系统设置缓存（VERSION = v2.0.0）
│   ├── config.py            # 配置 + Fernet 加密
│   ├── database.py          # 数据库引擎 + 自动迁移 + 重试提交
│   ├── models.py            # 数据模型（EmailAccount/LLMConfig/OCRConfig/RoutingRule 等）
│   ├── auth.py              # 认证 + 可配置用户名 + 登录频率限制
│   ├── csrf.py              # CSRF 保护（全 POST 覆盖）
│   ├── flash.py             # Flash 消息中间件
│   ├── services/            # 核心业务逻辑
│   │   ├── email_fetcher.py # 邮件拉取（163 raw socket / 标准 IMAP）+ 附件提取 + 黑名单过滤
│   │   ├── archive.py      # 压缩包附件解压（zip/tar/gz/bz2/xz 内置，rar/7z 走外部命令；仅分析链路使用，磁盘保留原始压缩包）+ 安全限制
│   │   ├── llm_analyzer.py  # 多阶段 LLM 分析（分组/类型识别/类型专属分析）+ 修订生成 + 上下文窗口探测
│   │   ├── scheduler.py     # 定时调度 + 转发决策 + 进度追踪 + 每日报告
│   │   ├── mail_forwarder.py# SMTP 转发 + Word 生成 + 模板填充 + 重试机制
│   │   ├── ocr.py           # OCR 识别 + 模型类型检测 + 响应异常处理 + PDF 直读
│   │   ├── mcp_client.py    # MCP 客户端：服务器配置解析、工具发现、工具调用循环
│   │   ├── prompt_budget.py # Token 预算管理：估算、优先级截断、上下文窗口对齐
│   │   ├── model_windows.py # 43+ 个已知 LLM 模型的上下文窗口表
│   │   └── ocr_test_data.py # 预制测试 PDF/PNG 数据，用于验证 OCR 配置
│   └── routes/
│       ├── __init__.py      # 路由注册（集中导入所有子路由）
│       ├── dashboard.py     # 仪表盘
│       ├── email_config.py  # 邮箱管理
│       ├── llm_config.py    # LLM 配置（含模型角色分配）
│       ├── ocr_config.py    # OCR 配置 + 连通性/PDF 能力检测
│       ├── routing.py       # 路由规则管理
│       ├── logs.py          # 处理日志（含发件人地址列）
│       ├── settings.py      # 系统设置 + 在线重启
│       ├── backup.py        # 配置备份/恢复
│       └── auto_update.py   # 自动更新：git 拉取 + Web UI 一键应用
├── templates/               # Jinja2 模板 + 审核意见书模板
├── static/                  # 静态资源
├── tests/                   # 测试套件
├── scripts/
│   └── generate_test_pdf.py # 生成预制测试 PDF 文件
├── LLM提示词模板.md           # 通用分析主模板（含 {analysis_instructions} 占位符）
├── 文书类型.conf               # 文书类型清单（类型识别阶段的候选列表）
├── 分析提示词/                # 各文书类型的独立分析流程提示词（每类型一个 .md）
├── run.py                   # Python 启动脚本（含管理命令）
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

### LLM 输出无法解析为 JSON

- 检查 LLM 配置的 `max_tokens` 是否过小（合同/协议等文书需完整输出修改版正文，建议 ≥ 4096）
- 系统内置 JSON 修复：自动转义 `revised_document` 中未转义的换行、清理尾逗号、提取 JSON 片段

### 压缩包附件无法解压

邮件附件为压缩包时系统自动解压用于分析（磁盘仍保留原始压缩包）。若处理日志出现 `未找到可用的解压工具` 或 `外部解压失败` 的 warning，按以下排查：

- **zip / tar / gz / bz2 / xz** — 内置支持，无需外部工具；失败通常是文件损坏，原附件保留不影响主流程
- **rar / 7z** — 依赖系统命令，容器内自检：

  ```bash
  docker exec legal-mail-router sh -c "which 7z unar lsar"
  ```

  - `rar` 解压走 `unrar` → `unar` → `7z`（p7zip 的 7z **不含 RAR 解码器**）；RAR5 格式必须由 `unar` 处理
  - `7z` 解压走 `7z` → `unar`
  - 缺少工具时：Docker 重建镜像（`docker compose build && docker compose up -d`，镜像内置 `p7zip-full` + `unar`）；宿主机运行需自行安装 7-Zip / unrar / unar
- 解压失败不会中断邮件处理：压缩包按原附件保留，日志记录 warning

## License

MIT
