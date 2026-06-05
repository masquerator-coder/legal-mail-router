# 文书分拣系统 (Legal Mail Router)

自动监控邮箱、AI 分析法律文书、智能转发到对应律师的邮件分拣系统。

## 功能特性

- **多邮箱监控** — 支持 163、Gmail、QQ 等主流邮箱，定时拉取新邮件
- **163.com 深度兼容** — 原始 SSL Socket + 缓冲 I/O，绕过 Python imaplib 限制
- **AI 智能分析** — 调用 LLM（OpenAI 兼容 API）识别文书类型、提取案号、关键日期、风险评估
- **多模态图片输入** — 支持图片附件直接送入多模态 LLM 分析
- **OCR 图片识别** — 支持图片附件文字识别（PaddleOCR / OpenAI Vision）
- **PDF 智能提取** — 文字型 PDF 直接提取，扫描件 PDF 自动 OCR + 多模态降级
- **附件文本提取** — 支持 PDF、DOCX、DOC（antiword）、XLSX、TXT 等格式
- **灵活路由规则** — 两层匹配：账户专属 → 全局兜底；LLM 类型 + 关键词双路径
- **关键词兜底** — 无 LLM 配置时仍可通过关键词匹配路由转发
- **多律师分发** — 按文书类型自动转发，附带 AI 分析摘要
- **发件人黑名单** — 可配置忽略指定发件人的邮件
- **重复邮件防护** — 基于 Message-ID 的批量查重
- **SMTP 自动推断** — 从 IMAP 域名自动推断同域 SMTP 配置（10 个服务商）
- **智能垃圾过滤** — LLM 置信度 <30% 的非法律文书自动跳过
- **修改版文书生成** — AI 分析完成后自动生成修订版文书，改动用颜色标注
- **文书模板管理** — 按文书类型 + 邮箱账户配置格式模板，同类型同账户唯一
- **上下文窗口探测** — 内置 43 个模型窗口表，支持一键自动探测
- **审核意见模板** — 使用 .docx 模板生成审核意见书，自动替换 xxx 占位符
- **三种 .docx 输出** — AI 分析报告、修改版文书、审核意见书
- **LLM 提示词自定义** — 可自定义分析提示词、修订提示词
- **Web 管理后台** — 可视化配置邮箱、LLM、路由、模板、系统参数
- **实时进度跟踪** — Web 轮询显示处理进度
- **配置备份恢复** — 一键导出 / 导入全量配置
- **每日运行报告** — 定时邮件汇总报告
- **服务在线重启** — Web 界面一键热重启（os.execv 原子替换）
- **数据库自动迁移** — 升级时自动补齐新增列
- **无界面独立运行** — 调度器后台自动运行

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

访问 `http://localhost:8888`，首次启动自动生成管理员密码（查看终端日志）。

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
  -p 8888:8888 \
  -v $(pwd)/data:/app/data \
  -e TZ=Asia/Shanghai \
  legal-mail-router

docker compose up -d
```

查看管理员密码：`docker logs legal-mail-router | grep 初始管理员密码`

### 数据持久化

| 文件 | 说明 |
|------|------|
| `data/legal_mail.db` | 所有配置和处理记录 |
| `data/.encryption_key` | 加密密钥，丢失无法解密密码 |
| `data/.session_secret` | Web 会话密钥，重启不失效 |
| `data/attachments/` | 邮件附件 |

> 跨机器迁移时须同时复制 `.encryption_key` 和 `.session_secret`。

## 首次配置

1. **系统设置** — 系统名称、监控天数、上下文窗口、输出方式等
2. **LLM 配置** — OpenAI 兼容 API 地址和 Key（支持 text / multimodal）
3. **邮箱配置** — 添加监控邮箱（IMAP + 授权码），配置发件人黑名单
4. **路由规则** — 文书类型到律师邮箱的转发规则（账户专属或全局）
5. **文书模板**（可选） — 为各文书类型配置格式模板（支持按邮箱区分）
6. **OCR 配置**（可选）

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
│   ├── config.py            # 配置 + Fernet 加密
│   ├── database.py          # 数据库引擎 + 自动迁移 + 重试提交
│   ├── models.py            # 数据模型（EmailAccount/LLMConfig/RoutingRule/DocTemplate 等）
│   ├── auth.py              # 认证 + 登录频率限制
│   ├── csrf.py              # CSRF 保护
│   ├── email_fetcher.py     # 邮件拉取（163 raw socket / 标准 IMAP）+ 附件提取
│   ├── llm_analyzer.py      # LLM 分析 + 修订生成 + 上下文窗口探测
│   ├── mail_forwarder.py    # SMTP 转发 + Word 生成 + 模板填充 + Markdown 清理
│   ├── ocr.py               # OCR 识别 + 模型类型检测
│   ├── scheduler.py         # 定时调度 + 进度追踪 + 路由匹配 + 模板加载
│   └── routes/
│       ├── dashboard.py     # 仪表盘
│       ├── email_config.py  # 邮箱管理
│       ├── llm_config.py    # LLM 配置
│       ├── ocr_config.py    # OCR 配置
│       ├── routing.py       # 路由规则
│       ├── doc_templates.py # 文书模板 CRUD（支持按账户区分）
│       ├── logs.py          # 处理日志
│       ├── settings.py      # 系统设置 + 在线重启（os.execv）
│       └── backup.py        # 配置备份 / 恢复
├── templates/               # Jinja2 模板（12 个页面）
├── static/                  # 静态资源
├── LLM提示词.md              # AI 分析自定义提示词
├── run.py                   # Python 启动脚本
├── start.sh                 # Shell 启动脚本
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

## License

MIT
