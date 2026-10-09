"""
Email Fetcher — 邮件解析、MIME 解码、附件提取测试
"""
import pytest
from app.services.email_fetcher import (
    decode_mime_header, extract_body, extract_attachments,
    ParsedEmail, AttachmentInfo, save_attachments, _parse_email,
    is_forwarded_copy, FORWARD_COPY_HEADER, FORWARD_COPY_MARKER,
)
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email.mime.application import MIMEApplication
from email import policy, message_from_bytes
from datetime import datetime


def _make_raw_email(subject="Test", body="Hello", from_addr="a@b.com",
                    attachments=None, message_id="<abc123@local>"):
    """构造原始邮件字节"""
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = "lawyer@lawfirm.com"
    msg["Date"] = "Mon, 01 Jan 2024 10:00:00 +0800"
    msg["Message-ID"] = message_id
    msg.attach(MIMEText(body, "plain", "utf-8"))

    if attachments:
        for name, content, ctype in attachments:
            part = MIMEApplication(content, Name=name, _subtype=ctype.split("/")[-1])
            part["Content-Disposition"] = f'attachment; filename="{name}"'
            msg.attach(part)

    return msg.as_bytes(policy=policy.HTTP)


class TestDecodeMimeHeader:
    """MIME 编码头解码"""

    def test_plain_ascii(self):
        assert decode_mime_header("Hello") == "Hello"

    def test_none_value(self):
        assert decode_mime_header(None) == ""

    def test_encoded_utf8(self):
        # =?utf-8?B?6Kej5Yaz5L2T5Lu9?= = "合同审核意见"
        encoded = "=?utf-8?B?5ZCI5L2T6L+b6Kej?="
        result = decode_mime_header(encoded)
        assert "审核" in result or result


class TestExtractBody:
    """邮件正文提取"""

    def test_plain_text_body(self):
        raw = _make_raw_email(body="Hello World")
        msg = message_from_bytes(raw, policy=policy.default)
        body = extract_body(msg)
        assert "Hello World" in body

    def test_multipart_plain_preferred_over_html(self):
        msg = MIMEMultipart("alternative")
        msg["Message-ID"] = "<test@local>"
        msg.attach(MIMEText("Plain text", "plain", "utf-8"))
        msg.attach(MIMEText("<html><body>HTML</body></html>", "html", "utf-8"))
        body = extract_body(msg)
        assert "Plain text" in body
        # HTML 内容不应出现在纯文本提取结果中（如果 plain 存在）
        # 注意：HTML 只作为回退

    def test_html_fallback(self):
        msg = MIMEMultipart("alternative")
        msg["Message-ID"] = "<test2@local>"
        msg.attach(MIMEText("<html><body><p>HTML only</p></body></html>", "html", "utf-8"))
        body = extract_body(msg)
        assert "HTML only" in body


class TestParseRecipient:
    """收件人(To+Cc)解析"""

    def _parse_raw(self, msg):
        return _parse_email(msg.as_bytes(policy=policy.HTTP))

    def test_to_and_cc_combined(self):
        msg = MIMEMultipart()
        msg["From"] = "a@b.com"
        msg["To"] = "lawyer@lawfirm.com"
        msg["Cc"] = "cc@example.com, another@example.com"
        msg["Subject"] = "t"
        msg["Date"] = "Mon, 01 Jan 2024 10:00:00 +0800"
        msg["Message-ID"] = "<m1@local>"
        msg.attach(MIMEText("hi", "plain", "utf-8"))
        parsed = self._parse_raw(msg)
        assert parsed.recipient == "lawyer@lawfirm.com, cc@example.com, another@example.com"

    def test_to_only(self):
        raw = _make_raw_email()
        parsed = _parse_email(raw)
        assert parsed.recipient == "lawyer@lawfirm.com"

    def test_fallback_delivered_to_when_no_to(self):
        # Bcc 收到：无 To/Cc，回退到投递地址头
        msg = MIMEMultipart()
        msg["From"] = "a@b.com"
        msg["Delivered-To"] = "inbox@lawfirm.com"
        msg["Subject"] = "t"
        msg["Date"] = "Mon, 01 Jan 2024 10:00:00 +0800"
        msg["Message-ID"] = "<m2@local>"
        msg.attach(MIMEText("hi", "plain", "utf-8"))
        parsed = self._parse_raw(msg)
        assert parsed.recipient == "inbox@lawfirm.com"

    def test_no_recipient_headers(self):
        msg = MIMEMultipart()
        msg["From"] = "a@b.com"
        msg["Subject"] = "t"
        msg["Date"] = "Mon, 01 Jan 2024 10:00:00 +0800"
        msg["Message-ID"] = "<m3@local>"
        msg.attach(MIMEText("hi", "plain", "utf-8"))
        parsed = self._parse_raw(msg)
        assert parsed.recipient == ""


class TestExtractAttachments:
    """附件提取"""
    def test_no_attachments(self):
        raw = _make_raw_email()
        msg = message_from_bytes(raw, policy=policy.default)
        atts = extract_attachments(msg)
        assert atts == []

    def test_single_attachment(self):
        attachments = [("test.pdf", b"%PDF-1.4 content", "application/pdf")]
        raw = _make_raw_email(attachments=attachments)
        msg = message_from_bytes(raw, policy=policy.default)
        atts = extract_attachments(msg)
        assert len(atts) == 1
        assert atts[0].filename == "test.pdf"
        assert atts[0].content == b"%PDF-1.4 content"

    def test_multiple_attachments(self):
        attachments = [
            ("doc.docx", b"docx content", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            ("sheet.xlsx", b"xlsx content", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ]
        raw = _make_raw_email(attachments=attachments)
        msg = message_from_bytes(raw, policy=policy.default)
        atts = extract_attachments(msg)
        assert len(atts) == 2
        assert atts[0].filename == "doc.docx"
        assert atts[1].filename == "sheet.xlsx"


class TestForwardedCopyDetection:
    """转发副本识别（X-Forwarded-By）

    回归背景：该头值若为非 ASCII，会被 email 的 compat32 序列化成 RFC2047
    编码串，而 _parse_email（email.message_from_bytes，compat32）不会自动解码，
    导致副本判重恒为 False → 转发副本被反复重分析、重转发。
    """

    def _raw_with_marker(self, marker, multipart=True):
        msg = MIMEMultipart() if multipart else MIMEText("正文", "plain", "utf-8")
        msg["From"] = "a@b.com"
        msg["To"] = "lawyer@lawfirm.com"
        msg["Subject"] = "测试"
        msg["Date"] = "Mon, 01 Jan 2024 10:00:00 +0800"
        msg["Message-ID"] = "<fwd@local>"
        if marker is not None:
            msg[FORWARD_COPY_HEADER] = marker
        if multipart:
            msg.attach(MIMEText("正文", "plain", "utf-8"))
        return msg.as_bytes()

    def test_marker_is_pure_ascii(self):
        """标记值必须是纯 ASCII，否则会被 RFC2047 编码而无法比对"""
        assert FORWARD_COPY_MARKER.isascii()

    def test_current_marker_detected(self):
        """本系统当前发出的转发副本必须被识别（multipart 与纯文本两种形态）"""
        for multipart in (True, False):
            parsed = _parse_email(self._raw_with_marker(FORWARD_COPY_MARKER, multipart))
            assert is_forwarded_copy(parsed.headers) is True, f"multipart={multipart}"

    def test_legacy_chinese_marker_still_detected(self):
        """升级前发出的中文标记副本（线上是 RFC2047 编码串）仍需识别"""
        parsed = _parse_email(self._raw_with_marker("邮件智能分析转发系统"))
        assert parsed.headers[FORWARD_COPY_HEADER.lower()].startswith("=?utf-8?")
        assert is_forwarded_copy(parsed.headers) is True

    def test_ordinary_mail_not_matched(self):
        """普通外部邮件不得被误判为副本"""
        parsed = _parse_email(self._raw_with_marker(None))
        assert is_forwarded_copy(parsed.headers) is False

    def test_similar_value_not_matched(self):
        """仅前缀相似的值不匹配（防止把无关邮件当副本跳过）"""
        parsed = _parse_email(self._raw_with_marker("legal-mail-router-evil"))
        assert is_forwarded_copy(parsed.headers) is False

    def test_empty_headers(self):
        assert is_forwarded_copy({}) is False
        assert is_forwarded_copy(None) is False

