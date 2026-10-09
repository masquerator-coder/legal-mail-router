"""
重新转发（/logs/{id}/resend）— 按目标跳过已成功送达的邮箱

回归背景：多目标转发「部分成功」时状态记为 failed，日志页会出现「重新转发」按钮；
原实现对 log.target_email 的全部目标无脑重发，导致已经收到的律师重复收到同一份文书，
且重发不写 ForwardRecord（不进「按转发目标邮箱的每日总结」统计）。
"""
# -*- coding: utf-8 -*-
import json

import pytest

from app.routes import logs as logs_module


class _Req:
    """最小 Request 替身：resend_email 只用它取 app.state.templates 与 session"""

    def __init__(self, csrf_token="tok"):
        self.app = type("_App", (), {"state": type("_S", (), {"templates": None})()})()
        self.session = {"_csrf_token": csrf_token}
        self.state = type("_State", (), {})()
        self.headers = {}


@pytest.fixture
def resend_db():
    """独立内存库（不触碰真实 data 目录）"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    from app.models import (  # noqa: F401  # 注册模型
        EmailAccount, EmailLog, Attachment, ForwardRecord, DefaultConfig,
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()
    engine.dispose()


def _seed(db, targets="a@x.com,b@x.com", status="failed",
          delivered=(), failed=()):
    """建立账户 + 日志 + 既有 ForwardRecord（均带加密字段占位值）"""
    from app.models import EmailAccount, EmailLog, ForwardRecord, DefaultConfig

    acc = EmailAccount(name="监控", imap_host="imap.example.com", username="m@x.com",
                       password_encrypted="enc")
    db.add(acc)
    db.flush()

    log = EmailLog(account_id=acc.id, message_id="<r1@local>", subject="主题",
                   sender="s@x.com", body_text="正文", status=status,
                   target_email=targets, doc_type="合同协议",
                   llm_raw_response=json.dumps([{"doc_type": "合同协议"}], ensure_ascii=False))
    db.add(log)
    db.flush()

    for email in delivered:
        db.add(ForwardRecord(log_id=log.id, target_email=email, success=True))
    for email in failed:
        db.add(ForwardRecord(log_id=log.id, target_email=email, success=False))

    # 默认 SMTP（含加密密码占位值；forward_email 会被打桩，不会真的解密）
    for key, value in (("default_smtp_host", "smtp.example.com"),
                       ("default_smtp_port", "587"),
                       ("default_smtp_username", "m@x.com"),
                       ("default_smtp_password", "enc")):
        db.add(DefaultConfig(key=key, value=value))
    db.commit()
    return log


@pytest.fixture
def stub_forward(monkeypatch):
    """打桩 forward_email，记录实际被发送的目标"""
    sent = []

    def _fake(**kwargs):
        sent.append(kwargs["to_email"])
        return True, ""

    monkeypatch.setattr(logs_module, "forward_email", _fake)
    return sent


def _run_resend(db, log_id):
    """直接调用端点函数（绕过 FastAPI 依赖注入，避免启动真实应用）"""
    import asyncio
    return asyncio.run(logs_module.resend_email(
        log_id=log_id, request=_Req(), db=db, form_csrf="tok",
    ))


class TestResendSkipsDeliveredTargets:

    def test_only_failed_target_is_resent(self, resend_db, stub_forward):
        """A 已成功、B 失败 → 重发只发给 B"""
        log = _seed(resend_db, targets="a@x.com,b@x.com", delivered=["a@x.com"])
        result = _run_resend(resend_db, log.id)

        assert stub_forward == ["b@x.com"]
        assert result["success"] is True
        assert "已跳过" in result["message"]
        assert resend_db.query(type(log)).filter_by(id=log.id).first().status == "forwarded"

    def test_records_new_forward_record(self, resend_db, stub_forward):
        """重发必须写 ForwardRecord（否则不进每日目标汇总）"""
        from app.models import ForwardRecord
        log = _seed(resend_db, targets="a@x.com,b@x.com", delivered=["a@x.com"])
        _run_resend(resend_db, log.id)

        rows = resend_db.query(ForwardRecord).filter_by(log_id=log.id).all()
        assert sorted((r.target_email, r.success) for r in rows) == [
            ("a@x.com", True), ("b@x.com", True),
        ]

    def test_all_delivered_is_not_resent(self, resend_db, stub_forward):
        """全部目标均已送达 → 一个都不再发（幂等，可反复点）"""
        from app.models import ForwardRecord
        log = _seed(resend_db, targets="a@x.com,b@x.com",
                    delivered=["a@x.com", "b@x.com"])
        result = _run_resend(resend_db, log.id)

        assert stub_forward == []
        assert result["success"] is True
        assert "无需重复转发" in result["message"]
        # 不得新增 ForwardRecord
        assert resend_db.query(ForwardRecord).filter_by(log_id=log.id).count() == 2

    def test_no_records_falls_back_to_full_resend(self, resend_db, stub_forward):
        """历史数据没有 ForwardRecord（升级前的失败记录）→ 仍按全部目标重发，不误判为已送达"""
        log = _seed(resend_db, targets="a@x.com,b@x.com")
        result = _run_resend(resend_db, log.id)

        assert stub_forward == ["a@x.com", "b@x.com"]
        assert result["success"] is True

    def test_only_failed_record_does_not_skip(self, resend_db, stub_forward):
        """前一轮失败的记录不构成「已送达」，仍应重发"""
        log = _seed(resend_db, targets="a@x.com", failed=["a@x.com"])
        _run_resend(resend_db, log.id)
        assert stub_forward == ["a@x.com"]

    def test_non_failed_status_still_rejected(self, resend_db, stub_forward):
        """非 failed 状态仍不允许重发（原有约束不变）"""
        log = _seed(resend_db, targets="a@x.com", status="forwarded", delivered=["a@x.com"])
        result = _run_resend(resend_db, log.id)
        assert result["success"] is False
        assert stub_forward == []
