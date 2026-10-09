"""
CSRF token 行为 — 校验通过后不得轮换会话内 token

回归背景：app/csrf.py 原先在每次校验通过后把 session 里的 token 换成新值，
而页面把 token 同时渲染进 <meta name="csrf-token"> 与表单隐藏域。一次轮换即让
同一浏览器其他页面/同页后续 fetch 的旧 token 失效 → 403，表现为「清除所有记录」
「修改密码」等按钮静默失败。

本测试用真实 CSRFMiddleware + SessionMiddleware + check_csrf 复刻
app/main.py 的中间件顺序（Session 最外层）。
"""
# -*- coding: utf-8 -*-
import pytest
from fastapi import FastAPI, Form, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app.csrf import CSRFMiddleware, CSRF_HEADER, CSRF_SESSION_KEY
from app.csrf import check_csrf, _get_or_create_token


@pytest.fixture
def client():
    """与 app/main.py:112-127 相同的中间件顺序与参数（secret 用测试值）"""
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(
        SessionMiddleware, secret_key="t" * 32, session_cookie="starze_session",
        max_age=86400, same_site="lax", https_only=False,
    )

    @app.get("/page")
    async def page(request: Request):
        """页面渲染：返回 session 内 token（= meta/隐藏域的值）"""
        return {"token": _get_or_create_token(request)}

    @app.post("/form")
    async def form(request: Request, form_csrf: str = Form("", alias="_csrf_token")):
        check_csrf(request, form_csrf)
        return {"ok": True}

    @app.post("/fetch")
    async def fetch(request: Request, form_csrf: str = Form("", alias="_csrf_token")):
        """模拟只带 header 的 fetch（如「清除所有记录」）"""
        check_csrf(request, form_csrf)
        return {"ok": True}

    return TestClient(app)


def _page_token(client):
    return client.get("/page").json()["token"]


class TestCsrfTokenNotRotated:

    def test_form_submit_does_not_rotate_token(self, client):
        token = _page_token(client)
        assert client.post("/form", data={"_csrf_token": token}).status_code == 200
        assert _page_token(client) == token

    def test_header_submit_does_not_rotate_token(self, client):
        token = _page_token(client)
        assert client.post("/fetch", headers={CSRF_HEADER: token}).status_code == 200
        assert _page_token(client) == token

    def test_form_then_header_with_same_token(self, client):
        """表单提交一次后，同页后续 fetch 仍用同一 token（本次回归的原始症状）"""
        token = _page_token(client)
        assert client.post("/form", data={"_csrf_token": token}).status_code == 200
        assert client.post("/fetch", headers={CSRF_HEADER: token}).status_code == 200

    def test_repeated_submissions_with_same_token(self, client):
        """反复提交（多次自动保存、批量删除）均有效"""
        token = _page_token(client)
        for _ in range(5):
            assert client.post("/form", data={"_csrf_token": token}).status_code == 200
            assert client.post("/fetch", headers={CSRF_HEADER: token}).status_code == 200


class TestCsrfStillRejects:

    def test_form_without_token_rejected(self, client):
        _page_token(client)
        assert client.post("/form", data={}).status_code == 403

    def test_form_with_wrong_token_rejected(self, client):
        _page_token(client)
        r = client.post("/form", data={"_csrf_token": "deadbeef"})
        assert r.status_code == 403

    def test_header_with_wrong_token_rejected(self, client):
        _page_token(client)
        r = client.post("/fetch", headers={CSRF_HEADER: "deadbeef"})
        assert r.status_code == 403

    def test_other_session_token_rejected(self, client):
        """另一个会话的 token 不得通过（token 与 session 绑定）"""
        token = _page_token(client)
        with TestClient(client.app) as other:
            assert other.post("/fetch", headers={CSRF_HEADER: token}).status_code == 403
