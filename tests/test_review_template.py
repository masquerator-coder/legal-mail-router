"""审查意见模板填充测试：AI 分析正文写入 + 标题按类型改写。"""
# -*- coding: utf-8 -*-
import os

import pytest
from docx import Document

from app.services.mail_forwarder import _fill_review_template


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


class TestInsertAnalysisBody:
    def test_analysis_text_written(self, tpl):
        out = _fill_review_template(
            template_path=tpl, analysis=ANALYSIS,
            original_subject="主题", sender="张三 <a@b.com>",
            attachment_filenames=["起诉状.docx"],
        )
        assert out
        txt = "\n".join(p.text for p in Document(out).paragraphs)
        assert "一、基本情况" in txt
        assert "民法典" in txt
        assert "建议补充合同原件" in txt
        os.remove(out)

    def test_title_rewritten_by_doc_type(self, tpl):
        out = _fill_review_template(
            template_path=tpl, analysis=ANALYSIS,
            original_subject="主题", sender="a@b.com",
            attachment_filenames=["起诉状.docx"],
        )
        assert out
        txt = "\n".join(p.text for p in Document(out).paragraphs)
        assert "起诉状审查意见" in txt
        assert "合同审核意见" not in txt.split("\n")[0]
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

    def test_no_analysis_keeps_template_intact(self, tpl):
        """无 ai_interpretation 时不应插入空段落或报错。"""
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

    def test_multiline_and_blank_line_split(self, tpl):
        """空行分段 + 段内换行都应保留为独立段落。"""
        out = _fill_review_template(
            template_path=tpl, analysis=ANALYSIS,
            original_subject="主题", sender="a@b.com",
            attachment_filenames=["起诉状.docx"],
        )
        assert out
        texts = [p.text.strip() for p in Document(out).paragraphs if p.text.strip()]
        assert "一、基本情况" in texts
        assert "二、法律依据" in texts
        assert "三、审查意见" in texts
        os.remove(out)

    def test_inserted_paragraphs_before_signature(self, tpl):
        """插入内容应位于落款日期之前，不破坏落款。"""
        out = _fill_review_template(
            template_path=tpl, analysis=ANALYSIS,
            original_subject="主题", sender="a@b.com",
            attachment_filenames=["起诉状.docx"],
        )
        assert out
        paras = [p.text.strip() for p in Document(out).paragraphs]
        idx_opinion = next(i for i, t in enumerate(paras) if t == "三、审查意见")
        idx_date = next(i for i, t in enumerate(paras) if "年" in t and "月" in t and "日" in t and len(t) < 20)
        assert idx_opinion < idx_date, "正文应插入在落款日期之前"
        os.remove(out)