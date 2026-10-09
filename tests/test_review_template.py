"""审查意见模板填充测试：以模板为主，仅填充 xxx 与落款日期。

生成结果必须与模板结构一致——不插入 AI 分析正文、不改写模板抬头，
否则「以模板为主」的约定会被静默破坏。
"""
# -*- coding: utf-8 -*-
import os
import re
from datetime import datetime, timedelta

import pytest
from docx import Document

from app.config import BASE_DIR
from app.services.mail_forwarder import (
    _extract_amount,
    _fill_review_template,
    _strip_amount_phrases,
)

# 模板落款日期断言容差：本地时区相对 UTC 最多偏移一天。
_DATE_TOLERANCE = timedelta(days=1)


def _assert_is_today(text: str):
    assert re.fullmatch(r"\d{4}年\d{2}月\d{2}日", text), text
    now = datetime.now()
    allowed = {
        (now + delta).strftime("%Y年%m月%d日")
        for delta in (-_DATE_TOLERANCE, timedelta(0), _DATE_TOLERANCE)
    }
    assert text in allowed, f"{text} 不是当天日期 {allowed}"


def _make_template(path, title, body):
    d = Document()
    d.add_paragraph(title)
    d.add_paragraph(body)
    d.add_paragraph('文本法律顾问已审核，不违反法律规定。')
    d.add_paragraph('')
    d.add_paragraph('')
    d.add_paragraph('')
    d.add_paragraph('2023年1月16日')
    d.save(str(path))
    return str(path)


@pytest.fixture
def tpl(tmp_path):
    return _make_template(
        tmp_path / "tpl.docx",
        "合同审核意见",
        "xxx年xxx月，法律顾问收到xxx发来的《xxx》。内容为xxx事宜。",
    )


ANALYSIS = {
    "doc_type": "起诉状",
    "case_summary": "原告主张支付工程款。",
    "involved_parties": "甲公司, 乙政府",
    "ai_interpretation": (
        "一、基本情况\n本案系建设工程款纠纷。\n\n"
        "二、法律依据\n依据《民法典》第五百七十七条。\n\n"
        "三、审查意见\n建议补充合同原件。"
    ),
}


class TestTemplateOnlyFill:
    def test_analysis_body_not_inserted(self, tpl):
        """AI 分析正文不得写入审查意见。"""
        out = _fill_review_template(
            template_path=tpl, analysis=ANALYSIS,
            original_subject="主题", sender="张三 <a@b.com>",
            attachment_filenames=["起诉状.docx"],
        )
        assert out
        txt = "\n".join(p.text for p in Document(out).paragraphs)
        assert "一、基本情况" not in txt
        assert "民法典" not in txt
        assert "建议补充合同原件" not in txt
        os.remove(out)

    def test_paragraph_count_matches_template(self, tpl):
        """段落数与模板一致（只填充，不增删段）。"""
        out = _fill_review_template(
            template_path=tpl, analysis=ANALYSIS,
            original_subject="主题", sender="a@b.com",
            attachment_filenames=["起诉状.docx"],
        )
        assert out
        assert len(Document(out).paragraphs) == len(Document(tpl).paragraphs) == 7
        os.remove(out)

    def test_title_kept_as_template(self, tpl):
        """抬头保持模板原文，不按文书类型改写。"""
        out = _fill_review_template(
            template_path=tpl, analysis=ANALYSIS,
            original_subject="主题", sender="a@b.com",
            attachment_filenames=["起诉状.docx"],
        )
        assert out
        first = Document(out).paragraphs[0].text
        assert first == "合同审核意见"
        assert "起诉状" not in first
        os.remove(out)

    def test_template_boilerplate_kept(self, tpl):
        """模板自带的措辞与落款不应被破坏。"""
        out = _fill_review_template(
            template_path=tpl, analysis=ANALYSIS,
            original_subject="主题", sender="a@b.com",
            attachment_filenames=["起诉状.docx"],
        )
        assert out
        txt = "\n".join(p.text for p in Document(out).paragraphs)
        assert "不违反法律规定" in txt
        assert "2026年" in txt          # 落款日期已更新
        os.remove(out)

    def test_xxx_placeholders_filled(self, tpl):
        """P1 的 xxx 全部被替换（无残留占位符）。"""
        out = _fill_review_template(
            template_path=tpl, analysis=ANALYSIS,
            original_subject="主题", sender="张三 <a@b.com>",
            attachment_filenames=["起诉状.docx"],
        )
        assert out
        p1 = Document(out).paragraphs[1].text
        assert "xxx" not in p1
        assert "张三" in p1              # 发来方
        assert "乙政府" in p1            # 拟签订方
        os.remove(out)

    def test_signature_date_is_last_paragraph(self, tmp_path):
        """落款日期取最后一段——与模板段数无关，n=2 的模板也正确。"""
        p = tmp_path / "tpl_short.docx"
        d = Document()
        d.add_paragraph("合同审核意见")
        d.add_paragraph("xxx年xxx月，法律顾问收到xxx发来的《xxx》。内容为xxx事宜。")
        d.add_paragraph("2023年1月16日")
        d.save(str(p))

        out = _fill_review_template(
            template_path=str(p), analysis=ANALYSIS,
            original_subject="主题", sender="a@b.com",
            attachment_filenames=["起诉状.docx"],
        )
        assert out
        assert len(Document(out).paragraphs) == 3
        _assert_is_today(Document(out).paragraphs[-1].text)
        os.remove(out)

    def test_no_analysis_keeps_template_intact(self, tpl):
        """无 ai_interpretation 时结果与模板结构一致。"""
        a = dict(ANALYSIS)
        a["ai_interpretation"] = ""
        out = _fill_review_template(
            template_path=tpl, analysis=a,
            original_subject="主题", sender="a@b.com",
            attachment_filenames=["起诉状.docx"],
        )
        assert out
        d = Document(out)
        assert len(d.paragraphs) == 7      # 与模板一致
        os.remove(out)

    def test_none_analysis_value(self, tpl):
        a = dict(ANALYSIS)
        a["ai_interpretation"] = None
        out = _fill_review_template(
            template_path=tpl, analysis=a,
            original_subject="主题", sender="a@b.com",
            attachment_filenames=["起诉状.docx"],
        )
        assert out
        assert len(Document(out).paragraphs) == 7
        os.remove(out)

    def test_missing_template_returns_none(self, tmp_path):
        assert _fill_review_template(
            template_path=str(tmp_path / "nope.docx"),
            analysis=ANALYSIS, original_subject="x",
        ) is None


class TestRealTemplates:
    """针对仓库真实模板的回归：抬头、固定措辞、页眉图章（章）必须保留。"""

    @pytest.mark.parametrize(
        "rel_path,expected_title",
        [
            ("templates/合同审核意见模板.docx", "合同审核意见"),
            ("templates/律师审核意见模板.docx", "律师审核意见"),
        ],
    )
    def test_real_template_keeps_title_and_seal(self, rel_path, expected_title):
        tpl = BASE_DIR / rel_path
        if not tpl.exists():
            pytest.skip(f"模板不存在: {rel_path}")

        src = Document(str(tpl))
        out = _fill_review_template(
            template_path=str(tpl), analysis=ANALYSIS,
            original_subject="主题", sender="张三 <a@b.com>",
            attachment_filenames=["某工程施工合同.docx"],
        )
        assert out
        try:
            got = Document(out)
            # 结构一致：只填充，不增删段
            assert len(got.paragraphs) == len(src.paragraphs)
            # 抬头保持模板原文
            assert got.paragraphs[0].text == expected_title
            # 固定措辞保留
            body = "\n".join(p.text for p in got.paragraphs)
            assert "法律顾问已审核，不违反法律规定" in body
            # 无 LLM 正文渗入
            assert "一、基本情况" not in body
            # 页眉图章（章）随模板保留：header 关系与图片部件都在
            import zipfile
            with zipfile.ZipFile(out) as z:
                names = z.namelist()
                assert any(n.startswith("word/header") for n in names)
                assert any(n.startswith("word/media/") for n in names)
                header = "".join(
                    z.read(n).decode("utf-8") for n in names
                    if re.fullmatch(r"word/header\d+\.xml", n)
                )
                assert "r:embed=" in header, "页眉图章图片引用丢失"
            # 落款日期
            _assert_is_today(got.paragraphs[-1].text)
        finally:
            os.remove(out)


class TestAmountExtraction:
    def test_thousands_separator_kept(self):
        """千分位不能被吃掉：「1,041,748元」→「1,041,748」。"""
        assert _extract_amount("结算价为1,041,748元") == "1,041,748"

    def test_wan_unit_kept(self):
        assert _extract_amount("合同价款30万元。") == "30万"

    def test_renminbi_prefix_dropped(self):
        assert _extract_amount("人民币12000元") == "12000"

    def test_currency_symbol_dropped(self):
        """`_extract_amount` 返回的是值本身，货币符号不入模板。"""
        assert _extract_amount("总价￥30万") == "30万"
        assert _extract_amount("价款¥12,000元") == "12,000"

    def test_missing_amount_fallback(self):
        assert _extract_amount("未提及金额") == "（待确认）"

    def test_strip_amount_phrases_is_phrase_only(self):
        """已知行为：只剥离「价款/金额」等**带标签**的表述，裸金额保留。

        模板填充另有 `_extract_amount` 单独取值填进「合同价款xxx元」，
        裸金额因此在样例里出现两次（模板 P1 与摘要末尾）。此处记录边界，
        不代表理想行为。
        """
        assert _strip_amount_phrases("，合同总金额30万元。") == ""
        assert _strip_amount_phrases("，价款30万元") == ""
        # 无标签的裸金额不会被剥离 —— 属于已知局限
        assert "1,041,748" in _strip_amount_phrases("结算价为1,041,748元并约定付款。")