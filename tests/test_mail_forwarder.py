"""
Mail Forwarder — 转发正文构建测试（原发件人/原收件人/原收件日期/原主题注明）
"""
import pytest
from datetime import datetime
from app.services.mail_forwarder import _build_email_body


class TestBuildEmailBodyMeta:
    """转发正文中的原邮件元信息"""

    def test_includes_sender_recipient_subject(self):
        body = _build_email_body(
            "张三",
            [{"doc_type": "起诉状", "urgency": "high"}],
            "合同纠纷案",
            original_sender="甲方 <jia@example.com>",
            original_recipient="乙方 <yi@example.com>, 丙方 <bing@example.com>",
        )
        assert "原发件人：甲方 <jia@example.com>" in body
        assert "原收件人：乙方 <yi@example.com>, 丙方 <bing@example.com>" in body
        assert "原邮件主题：合同纠纷案" in body
        # 顺序：发件人 → 收件人 → 主题
        assert body.index("原发件人") < body.index("原收件人") < body.index("原邮件主题")

    def test_includes_received_date(self):
        body = _build_email_body(
            "张三", [{"doc_type": "起诉状"}], "主题X",
            original_sender="a@b.com",
            original_recipient="c@d.com",
            original_date=datetime(2024, 1, 1, 10, 30),
        )
        assert "原收件日期：2024-01-01 10:30" in body
        # 日期位于收件人与主题之间
        assert body.index("原收件人") < body.index("原收件日期") < body.index("原邮件主题")

    def test_received_date_str_passthrough(self):
        body = _build_email_body(
            "张三", [{"doc_type": "起诉状"}], "主题X",
            original_date="2024-06-05 08:00",
        )
        assert "原收件日期：2024-06-05 08:00" in body

    def test_omits_missing_meta(self):
        body = _build_email_body("张三", [{"doc_type": "起诉状"}], "主题X")
        assert "原发件人" not in body
        assert "原收件人" not in body
        assert "原收件日期" not in body
        assert "原邮件主题：主题X" in body

    def test_omits_none_date(self):
        # log.received_at 为 NULL 的真实路径
        body = _build_email_body(
            "张三", [{"doc_type": "起诉状"}], "主题X", original_date=None,
        )
        assert "原收件日期" not in body

    def test_aware_datetime_converted_to_local(self):
        # 跨时区邮件（UTC+0）：应转换为本地时区时间
        from datetime import timezone, timedelta
        utc_dt = datetime(2024, 1, 1, 10, 0, tzinfo=timezone.utc)
        body = _build_email_body(
            "张三", [{"doc_type": "起诉状"}], "主题X", original_date=utc_dt,
        )
        expected_local = utc_dt.astimezone().strftime("%Y-%m-%d %H:%M")
        assert f"原收件日期：{expected_local}" in body
        # 本地时区非 UTC 时，不应显示原始 UTC 墙钟时间（UTC 下二者相同，跳过该负向断言）
        if utc_dt.astimezone().strftime("%H:%M") != "10:00":
            assert "原收件日期：2024-01-01 10:00" not in body

    def test_brief_mode_includes_meta(self):
        body = _build_email_body(
            "张三", [{"doc_type": "起诉状"}], "主题X", brief_mode=True,
            original_sender="a@b.com", original_recipient="c@d.com",
            original_date=datetime(2024, 3, 15, 9, 5),
        )
        assert "原发件人：a@b.com" in body
        assert "原收件人：c@d.com" in body
        assert "原收件日期：2024-03-15 09:05" in body
        assert "原邮件主题：主题X" in body


class TestParseRevisionMarkers:
    """修订标记解析（含对 LLM 不规范输出的容错）"""

    def test_normal_paired_markers(self):
        from app.services.mail_forwarder import _parse_revision_markers as parse
        segs = parse("原文。【新增】补充【/新增】继续。【删除】旧条款【/删除】")
        assert segs == [("normal", "原文。"), ("add", "补充"),
                        ("normal", "继续。"), ("delete", "旧条款")]

    def test_unclosed_marker_extends_to_end(self):
        """开标记未闭合：着色延续到文本末尾（隐式闭合）"""
        from app.services.mail_forwarder import _parse_revision_markers as parse
        segs = parse("【新增】1. 全部内容。\n2. 延续未闭合。")
        assert segs == [("add", "1. 全部内容。\n2. 延续未闭合。")]

    def test_prefix_style_markers_merge(self):
        """行首前缀式【新增】（逐段无闭合）应合并为同一新增段"""
        from app.services.mail_forwarder import _parse_revision_markers as parse
        segs = parse("【新增】1. 第一条。\n【新增】2. 第二条。")
        assert segs == [("add", "1. 第一条。\n2. 第二条。")]

    def test_nested_same_type_tolerated(self):
        """同类型嵌套 + 外层缺闭合：内层闭合后延续仍为新增"""
        from app.services.mail_forwarder import _parse_revision_markers as parse
        segs = parse("【新增】A【新增】10个工作日【/新增】支付至账户。")
        assert segs == [("add", "A10个工作日支付至账户。")]

    def test_mismatched_close_ignored(self):
        """闭合标记与当前类型不匹配时忽略，维持当前着色"""
        from app.services.mail_forwarder import _parse_revision_markers as parse
        segs = parse("【修改】改动【/新增】后续")
        assert segs == [("modify", "改动后续")]

    def test_marker_text_not_in_output(self):
        """标记文字本身不进入输出段"""
        from app.services.mail_forwarder import _parse_revision_markers as parse
        segs = parse("【新增】内容【/新增】")
        assert segs == [("add", "内容")]
        assert all("【" not in s for _, s in segs)

    def test_business_placeholder_not_marker(self):
        """原文中的【待确认】等业务占位符不是修订标记，原样保留"""
        from app.services.mail_forwarder import _parse_revision_markers as parse
        segs = parse("【新增】总费用【待确认】元。【/新增】")
        assert segs == [("add", "总费用【待确认】元。")]

    def test_no_markers_returns_normal(self):
        from app.services.mail_forwarder import _parse_revision_markers as parse
        assert parse("纯文本") == [("normal", "纯文本")]


class TestDedupeSmtpCfgs:
    """SMTP 候选列表去重"""

    def test_filters_none_and_dedupes(self):
        from app.services.mail_forwarder import dedupe_smtp_cfgs

        base = {"host": "smtp.qq.com", "port": 465, "username": "a@qq.com",
                "password_encrypted": "enc1"}
        same = dict(base)
        other = {"host": "smtp.163.com", "port": 587, "username": "b@163.com",
                 "password_encrypted": "enc2"}
        result = dedupe_smtp_cfgs([None, base, same, other, None])
        assert result == [base, other]

    def test_same_host_different_credential_kept(self):
        from app.services.mail_forwarder import dedupe_smtp_cfgs

        cfg1 = {"host": "smtp.qq.com", "port": 465, "username": "a@qq.com",
                "password_encrypted": "enc1"}
        cfg2 = {"host": "smtp.qq.com", "port": 465, "username": "a@qq.com",
                "password_encrypted": "enc2"}
        assert dedupe_smtp_cfgs([cfg1, cfg2]) == [cfg1, cfg2]

    def test_empty_list(self):
        from app.services.mail_forwarder import dedupe_smtp_cfgs

        assert dedupe_smtp_cfgs([]) == []
        assert dedupe_smtp_cfgs([None, None]) == []


class TestForwardedCopyHeader:
    """转发邮件必须带 ASCII 的 X-Forwarded-By 标记，且经序列化/解析后仍能识别

    回归背景：标记值原为中文，compat32 序列化成 RFC2047 编码串，
    接收端（email.message_from_bytes）不解码 → 副本判重恒 False →
    转发副本被反复重分析、重转发。
    """

    def _send_and_capture(self, monkeypatch, **overrides):
        from app.services import mail_forwarder as mf

        captured = {}

        class _FakeSMTP:
            def __init__(self, *a, **kw):
                pass

            def starttls(self, *a, **kw):
                pass

            def login(self, *a, **kw):
                pass

            def sendmail(self, from_addr, to_addrs, msg_text):
                captured["from"] = from_addr
                captured["to"] = to_addrs
                captured["raw"] = msg_text

            def quit(self):
                pass

        monkeypatch.setattr(mf.smtplib, "SMTP", _FakeSMTP)
        monkeypatch.setattr(mf.smtplib, "SMTP_SSL", _FakeSMTP)
        monkeypatch.setattr(mf, "decrypt", lambda v: "smtp-password", raising=False)

        kwargs = dict(
            smtp_host="smtp.example.com", smtp_port=587, smtp_username="m@x.com",
            smtp_password_encrypted="enc", from_email="m@x.com",
            to_email="lawyer@example.com", to_name="张律师",
            original_subject="关于合同的通知", original_body="正文",
            analyses_results=[{"doc_type": "合同协议", "confidence": 0.9}],
            retry_count=0,
        )
        kwargs.update(overrides)
        success, err = mf.forward_email(**kwargs)
        assert success is True, f"转发失败: {err}"
        return captured["raw"]

    def test_header_is_ascii_marker(self, monkeypatch):
        from app.services.email_fetcher import (
            FORWARD_COPY_MARKER, is_forwarded_copy, _parse_email,
        )
        raw = self._send_and_capture(monkeypatch)
        assert "X-Forwarded-By: " in raw
        header_line = [ln for ln in raw.splitlines()
                       if ln.lower().startswith("x-forwarded-by")][0]
        assert header_line.split(":", 1)[1].strip() == FORWARD_COPY_MARKER
        assert FORWARD_COPY_MARKER.isascii()

    def test_roundtrip_is_recognized_as_copy(self, monkeypatch):
        """端到端闭环比对：发出的邮件被接收端解析后必须判为「转发副本」"""
        from app.services.email_fetcher import is_forwarded_copy, _parse_email
        raw = self._send_and_capture(monkeypatch)
        parsed = _parse_email(raw.encode("utf-8"))
        assert is_forwarded_copy(parsed.headers) is True
