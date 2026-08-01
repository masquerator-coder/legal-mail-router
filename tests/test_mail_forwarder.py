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
