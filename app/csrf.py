"""
CSRF 保护

- HTML 表单: _csrf_token 隐藏域 → 路由手动调用 check_csrf() 验证
- JS fetch: X-CSRF-Token header → 中间件自动验证
"""

import secrets
from fastapi import Request, HTTPException

CSRF_SESSION_KEY = "_csrf_token"
CSRF_FORM_FIELD = "_csrf_token"
CSRF_HEADER = "X-CSRF-Token"
EXEMPT_PREFIXES = ("/health", "/favicon.ico", "/static")


def _get_or_create_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_hex(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def csrf_token_input(request: Request) -> str:
    token = _get_or_create_token(request)
    return f'<input type="hidden" name="{CSRF_FORM_FIELD}" value="{token}">'


def csrf_token_value(request: Request) -> str:
    return _get_or_create_token(request)


def check_csrf(request: Request, submitted_token: str = ""):
    """路由中调用：验证 CSRF token（支持表单隐藏域和 X-CSRF-Token header）

    表单提交：submitted_token 为 _csrf_token 表单字段值
    JS fetch：X-CSRF-Token header（中间件已验证，此处双重确认）

    注意：校验通过后**不轮换**会话内的 token。页面把 token 同时渲染进
    <meta name="csrf-token"> 与表单隐藏域，一次轮换会让同一浏览器其他页面
    （或同页后续 fetch）持有的旧 token 立即失效并返回 403 —— 表现为
    「清除所有记录」「修改密码」等按钮静默失败，必须刷新页面才能恢复。
    防重放不依赖轮换：token 为 32 字节随机值，跨站请求由会话 cookie 的
    SameSite=Lax + token 比对共同拦截。
    """
    session_token = request.session.get(CSRF_SESSION_KEY)
    if not session_token:
        raise HTTPException(status_code=403, detail="Session 过期，请刷新页面")

    # 优先：中间件已验证过 header → 直接放行
    if getattr(request.state, "_csrf_validated", False):
        return

    # 检查 header（兜底，正常情况中间件已处理）
    header_token = request.headers.get(CSRF_HEADER)
    if header_token and secrets.compare_digest(session_token, header_token):
        return

    # 检查表单隐藏域
    if not submitted_token or not secrets.compare_digest(session_token, submitted_token):
        raise HTTPException(status_code=403, detail="CSRF 验证失败，请刷新页面后重试")


# ── ASGI Middleware —— 仅处理 JS fetch header ──

from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from starlette.responses import PlainTextResponse  # noqa: E402


class CSRFMiddleware(BaseHTTPMiddleware):
    """验证 X-CSRF-Token header（JS fetch 调用）。验证通过后在 request.state 设标记。"""

    async def dispatch(self, request: Request, call_next):
        if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
            return await call_next(request)

        path = request.url.path
        if any(path.startswith(p) for p in EXEMPT_PREFIXES):
            return await call_next(request)

        header_token = request.headers.get(CSRF_HEADER)
        if header_token:
            if not request.session:
                return await call_next(request)
            session_token = request.session.get(CSRF_SESSION_KEY)
            if not session_token or not secrets.compare_digest(session_token, header_token):
                return PlainTextResponse("CSRF 验证失败", status_code=403)
            # 标记已验证（不轮换 token，避免页面 JS 中缓存的 token 失效）
            request.state._csrf_validated = True

        return await call_next(request)
