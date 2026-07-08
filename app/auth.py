"""
认证模块 — 登录/登出、密码管理、认证中间件
"""
import hashlib
import os
import time
import logging
from collections import defaultdict
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import DefaultConfig
from app.csrf import check_csrf

logger = logging.getLogger(__name__)

router = APIRouter(tags=["认证"])

# 白名单路径（无需登录即可访问）
AUTH_WHITELIST = {"/login", "/health", "/favicon.ico"}
AUTH_PREFIX_WHITELIST = {"/static/"}


def _get_client_ip(request: Request) -> str:
    """获取真实客户端 IP，兼容反向代理场景

    优先级：X-Forwarded-For > X-Real-IP > request.client.host
    """
    # X-Forwarded-For: client_ip, proxy1, proxy2, ...
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        # 取最左端的真实客户端 IP
        first_ip = forwarded.split(",")[0].strip()
        if first_ip:
            return first_ip

    # X-Real-IP: Nginx 等代理设置的直通头部
    real_ip = request.headers.get("X-Real-IP", "")
    if real_ip:
        return real_ip.strip()

    # 直连场景
    return request.client.host if request.client else "unknown"

# ── 登录频率限制（内存计数器）──

_LOGIN_LOCKOUT_WINDOW = 900      # 15 分钟（秒）
_LOGIN_MAX_ATTEMPTS = 5          # 最大尝试次数
_login_attempts: dict[str, list[float]] = defaultdict(list)  # client_ip → [timestamp, ...]


def _check_login_rate_limit(client_ip: str):
    """检查登录频率，超过限制则抛出 HTTPException"""
    now = time.time()
    attempts = _login_attempts[client_ip]
    # 清理窗口外的旧记录
    cutoff = now - _LOGIN_LOCKOUT_WINDOW
    attempts[:] = [t for t in attempts if t > cutoff]
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        retry_after = int(attempts[0] + _LOGIN_LOCKOUT_WINDOW - now)
        logger.warning(f"登录频率超限: {client_ip}")
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过于频繁，请 {max(1, retry_after)} 秒后再试",
        )


def _record_login_failure(client_ip: str):
    """记录一次失败的登录尝试"""
    _login_attempts[client_ip].append(time.time())


def _reset_login_rate_limit(client_ip: str):
    """登录成功后清除计数"""
    _login_attempts.pop(client_ip, None)

# ── 密码哈希 ──

def hash_password(password: str, salt: bytes = None) -> str:
    """PBKDF2-SHA256 哈希密码，返回 salt:hash 格式"""
    if salt is None:
        salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return salt.hex() + ":" + key.hex()


def verify_password(password: str, stored: str) -> bool:
    """验证密码"""
    try:
        salt_hex, key_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        expected = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
        return expected.hex() == key_hex
    except Exception:
        return False


def get_stored_password_hash(db: Session) -> str:
    """从数据库读取密码哈希，无则返回空"""
    cfg = db.query(DefaultConfig).filter_by(key="admin_password").first()
    return cfg.value if cfg else ""


def get_stored_admin_username(db: Session) -> str:
    """从数据库读取管理员用户名，默认 'admin'"""
    cfg = db.query(DefaultConfig).filter_by(key="admin_username").first()
    return cfg.value.strip() if cfg and cfg.value else "admin"


def init_admin_password(db: Session):
    """初始化默认管理员密码（如不存在）"""
    existing = db.query(DefaultConfig).filter_by(key="admin_password").first()
    if not existing:
        import random
        import string
        # 生成随机密码
        chars = string.ascii_letters + string.digits
        default_pw = ''.join(random.choice(chars) for _ in range(12))
        hashed = hash_password(default_pw)
        db.add(DefaultConfig(key="admin_password", value=hashed))
        # 同时生成一个 token 作为初始 API token（备用）
        token = ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(32))
        db.add(DefaultConfig(key="admin_token", value=token))
        db.commit()
        logger.warning("============================================")
        logger.warning("  初始管理员密码已生成，请登录后立即修改！")
        logger.warning("============================================")
        # 同时输出到 stdout，让非日志用户也能看到
        print("\n" + "=" * 50, flush=True)
        print(f"  ⚖️  文书分拣系统 首次启动", flush=True)
        print(f"  管理员用户名: admin", flush=True)
        print(f"  初始密码:     {default_pw}", flush=True)
        print(f"  ⚠️  请登录后立即修改密码！", flush=True)
        print("=" * 50 + "\n", flush=True)
        return default_pw
    # 确保 admin_username 存在
    username_cfg = db.query(DefaultConfig).filter_by(key="admin_username").first()
    if not username_cfg:
        db.add(DefaultConfig(key="admin_username", value="admin"))
        db.commit()
    return None


# ── 路由 ──

@router.get("/login")
async def login_page(request: Request):
    """登录页面"""
    return request.app.state.templates.TemplateResponse(request, "login.html", {
        "request": request,
        "error": request.query_params.get("error", ""),
    })


@router.post("/login")
async def login(
    request: Request,
    db: Session = Depends(get_db),
    username: str = Form(""),
    password: str = Form(""),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """处理登录"""
    check_csrf(request, form_csrf)

    # 频率限制（根据客户端 IP）
    client_ip = _get_client_ip(request)
    try:
        _check_login_rate_limit(client_ip)
    except HTTPException:
        return request.app.state.templates.TemplateResponse(request, "login.html", {
            "request": request,
            "error": "登录尝试过于频繁，请 15 分钟后再试",
        })

    stored = get_stored_password_hash(db)
    if not stored:
        # 无密码 → 初始化
        init_admin_password(db)
        stored = get_stored_password_hash(db)

    admin_username = get_stored_admin_username(db)
    if username != admin_username or not verify_password(password, stored):
        _record_login_failure(client_ip)
        return request.app.state.templates.TemplateResponse(request, "login.html", {
            "request": request,
            "error": "用户名或密码错误",
        })

    # 登录成功
    _reset_login_rate_limit(client_ip)
    request.session["authenticated"] = True
    request.session["auth_time"] = time.time()

    # 清除可能的旧错误信息
    return RedirectResponse(url="/dashboard", status_code=303)


@router.get("/logout")
async def logout(request: Request):
    """登出"""
    request.session.clear()
    return RedirectResponse(url="/login?error=您已退出登录", status_code=303)


# ── 认证中间件（纯 ASGI，兼容 SessionMiddleware）──

class AuthMiddleware:
    """认证中间件 — 拦截未登录请求（纯 ASGI，不消费请求体）"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")

        # 白名单直接放行
        if path in AUTH_WHITELIST:
            await self.app(scope, receive, send)
            return
        for prefix in AUTH_PREFIX_WHITELIST:
            if path.startswith(prefix):
                await self.app(scope, receive, send)
                return

        # 检查 session 中的登录状态
        session = scope.get("session", {})
        if not session.get("authenticated"):
            # API 请求返回 401
            if path.startswith("/settings/") and path.endswith("/change-password"):
                pass  # 由路由自己处理认证
            elif any(path.startswith(p) for p in ["/dashboard/", "/logs/", "/email-config/", "/llm-config/", "/ocr-config/", "/routing/"]):
                from starlette.responses import JSONResponse
                response = JSONResponse({"success": False, "message": "未登录"}, status_code=401)
                await response(scope, receive, send)
                return

            # 页面请求 → 重定向到登录页
            from starlette.responses import RedirectResponse
            response = RedirectResponse(url="/login?error=请先登录", status_code=302)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


# ── 修改密码 ──

@router.post("/settings/change-password")
async def change_password(
    request: Request,
    db: Session = Depends(get_db),
    old_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """修改管理员密码"""
    check_csrf(request, form_csrf)

    # 验证
    if not new_password or len(new_password) < 6:
        return JSONResponse({"success": False, "message": "新密码至少 6 位"}, status_code=400)

    if new_password != confirm_password:
        return JSONResponse({"success": False, "message": "两次输入的新密码不一致"}, status_code=400)

    stored = get_stored_password_hash(db)
    if not verify_password(old_password, stored):
        return JSONResponse({"success": False, "message": "原密码不正确"}, status_code=400)

    # 更新密码
    new_hash = hash_password(new_password)
    cfg = db.query(DefaultConfig).filter_by(key="admin_password").first()
    if cfg:
        cfg.value = new_hash
    else:
        db.add(DefaultConfig(key="admin_password", value=new_hash))
    db.commit()

    logger.info("管理员密码已修改")
    return {"success": True, "message": "密码修改成功"}
