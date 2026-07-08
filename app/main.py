"""
文书邮件分拣系统 — FastAPI 主应用
"""
import logging
import os
import base64
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import init_db
from app.scheduler import start_scheduler, shutdown_scheduler, scheduler, schedule_cleanup_job, schedule_daily_report_job
from app.config import BASE_DIR, load_system_settings, SYSTEM_NAME

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── 持久化 Session 密钥 ──
DATA_DIR = BASE_DIR / "data"
os.makedirs(DATA_DIR, exist_ok=True)
SESSION_KEY_FILE = DATA_DIR / ".session_secret"


def _load_or_create_session_key():
    if SESSION_KEY_FILE.exists():
        key = SESSION_KEY_FILE.read_bytes()
        if len(key) >= 32:
            logger.info("已加载持久化 session 密钥")
            return key[:32]
    key = os.urandom(32)
    SESSION_KEY_FILE.write_bytes(key)
    try:
        SESSION_KEY_FILE.chmod(0o600)
    except Exception:
        pass  # Windows 下 chmod 可能不支持，忽略
    logger.info("已生成新的 session 密钥并持久化保存")
    return key


_session_key = _load_or_create_session_key()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    logger.info("⚖️  文书分拣系统 启动中...")
    init_db()
    logger.info("数据库初始化完成")

    # 加载系统设置缓存
    load_system_settings()
    logger.info(f"系统名称: {SYSTEM_NAME}")

    # 加载已有邮箱账户的调度任务
    from app.database import SessionLocal
    from app.models import EmailAccount
    from app.scheduler import add_check_job

    db = SessionLocal()
    try:
        accounts = db.query(EmailAccount).filter_by(enabled=True).all()
        for acc in accounts:
            add_check_job(acc.id, acc.check_interval)
        logger.info(f"已加载 {len(accounts)} 个邮箱监控任务")
    finally:
        db.close()

    start_scheduler()
    schedule_cleanup_job()
    schedule_daily_report_job()
    logger.info("调度引擎已启动")

    # 初始化管理员密码（首次运行生成随机密码）
    db2 = SessionLocal()
    try:
        init_admin_password(db2)
    finally:
        db2.close()

    yield

    # 关闭时
    shutdown_scheduler()
    logger.info("系统已关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title="文书分拣系统",
    description="自动监控邮箱、AI分析文书类型、智能转发到对应律师",
    version="1.0.0",
    lifespan=lifespan,
)

# CSRF 保护中间件
from app.csrf import CSRFMiddleware  # noqa: E402
app.add_middleware(CSRFMiddleware)

# 认证中间件
from app.auth import AuthMiddleware, init_admin_password  # noqa: E402
app.add_middleware(AuthMiddleware)

# 会话中间件（必须在最外层，最先执行，为内层提供 session）
from starlette.middleware.sessions import SessionMiddleware  # noqa: E402
app.add_middleware(
    SessionMiddleware,
    secret_key=base64.b64encode(_session_key).decode(),
    session_cookie="starze_session",
    max_age=86400,          # 24 小时过期
    same_site="lax",        # 限制跨站请求
    https_only=os.environ.get("FORCE_HTTPS", "").lower() in ("true", "1", "yes"),
)

# 静态文件
static_dir = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# 模板引擎 (Python 3.14 兼容: 创建无缓存的 Environment)
templates_dir = BASE_DIR / "templates"
from jinja2 import Environment, FileSystemLoader  # noqa: E402
env = Environment(loader=FileSystemLoader(str(templates_dir)), cache_size=0)
templates = Jinja2Templates(env=env)
app.state.templates = templates

# 注册 flash 消息全局函数
from app.flash import get_flash_messages  # noqa: E402
env.globals["get_flash_messages"] = get_flash_messages
# CSRF 保护 — 所有表单自动注入 token
from app.csrf import csrf_token_input, csrf_token_value  # noqa: E402
env.globals["csrf_token_input"] = csrf_token_input
env.globals["csrf_token_value"] = csrf_token_value
# 系统名称全局变量（供模板使用）
env.globals["system_name"] = SYSTEM_NAME

# 发件人解析过滤器：将 "Name <email>" 或纯邮箱解析为 【名称】【邮箱】
def format_sender(sender_raw: str) -> dict:
    """
    解析邮件发件人，返回 {"name": str, "email": str}
    支持格式:
      - Name <email@example.com>
      - =?utf-8?B?...?= <email@example.com>  (已解码后传入)
      - email@example.com  (仅邮箱)
    """
    import re
    if not sender_raw:
        return {"name": "", "email": ""}

    # 匹配 <email> 格式的邮箱地址
    email_match = re.search(r'<([^>]+)>', sender_raw)
    if email_match:
        email = email_match.group(1).strip()
        # 名称为去掉 <email> 后的部分
        name = sender_raw[:email_match.start()].strip()
        # 清理可能的引号
        name = name.strip('"').strip("'").strip()
        return {"name": name or "", "email": email}
    else:
        # 纯邮箱地址
        return {"name": "", "email": sender_raw.strip()}

env.filters["format_sender"] = format_sender

# 注册路由
from app.routes import (  # noqa: E402
    dashboard_router,
    email_router,
    llm_router,
    routing_router,
    logs_router,
    ocr_router,
    settings_router,
    backup_router,
    doc_templates_router,
    auto_update_router,
)

app.include_router(dashboard_router)
app.include_router(email_router)
app.include_router(llm_router)
app.include_router(routing_router)
app.include_router(logs_router)
app.include_router(ocr_router)
app.include_router(settings_router)
app.include_router(backup_router)
app.include_router(doc_templates_router)
app.include_router(auto_update_router)

# 注册认证路由
from app.auth import router as auth_router  # noqa: E402
app.include_router(auth_router)


@app.get("/")
async def root():
    """重定向到仪表盘"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/dashboard")


@app.get("/health")
async def health():
    """健康检查端点"""
    return {
        "status": "healthy",
        "scheduler": "running" if scheduler.running else "stopped",
    }


@app.get("/favicon.ico")
async def favicon():
    """浏览器标签页图标"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/static/favicon.ico")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8020, reload=False)
