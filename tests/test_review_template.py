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
    _fill_review_template,
    _review_template_path,
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
    """合同协议模板形态：命名占位符 + xxxx年x月x日 + 留空落款位。"""
    return _make_template(
        tmp_path / "合同协议审核意见模板.docx",
        "合同审核意见",
        "xxxx年x月x日，法律顾问收到【合同甲方】发来的拟与【合同相对方】"
        "签订的【合同正文名称】。合同内容为【合同内容】事宜。合同价款【合同价款】元。",
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
    # 审查意见模板填充字段（均取自送审文书正文）
    "contract_party_a": "天津某某建设集团有限公司",
    "contract_party_b": "某某设备制造有限公司",
    "contract_name": "设备采购合同书",
    "contract_content": "采购施工升降机设备",
    "contract_amount": "1,041,748",
    "agency_name": "天津市某某区人民政府",
    "document_title_no": "《关于某某事项的申请书》（津某信〔2026〕12号）",
}

PLACEHOLDER_RE = r"【[^】]{1,30}】"


def _text(out) -> str:
    return "\n".join(p.text for p in Document(out).paragraphs)


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

    def test_named_placeholders_filled(self, tpl):
        """命名占位符全部被替换（无【】残留）。"""
        out = _fill_review_template(
            template_path=tpl, analysis=ANALYSIS,
            original_subject="主题", sender="张三 <a@b.com>",
            attachment_filenames=["起诉状.docx"],
        )
        assert out
        p1 = Document(out).paragraphs[1].text
        assert not re.search(PLACEHOLDER_RE, p1), p1
        assert "天津某某建设集团有限公司" in p1     # 甲方
        assert "某某设备制造有限公司" in p1         # 相对方
        assert "设备采购合同书" in p1               # 合同正文名称
        assert "采购施工升降机设备" in p1           # 合同内容
        assert "1,041,748" in p1                    # 合同价款
        os.remove(out)

    def test_party_names_not_from_sender_or_subject(self, tpl):
        """甲乙方/合同名取自文书正文，**不得**回退邮件标题或发件人。

        回归：旧实现把发件人填进价款位、把月份填进合同内容位（见
        test_placeholder_values_do_not_shift 中的错位用例）。
        """
        out = _fill_review_template(
            template_path=tpl, analysis=ANALYSIS,
            original_subject="【邮件标题】某公司设备采购合同",
            sender="李四 <lisi@example.com>",
            attachment_filenames=["某附件名.docx"],
        )
        assert out
        p1 = Document(out).paragraphs[1].text
        assert "李四" not in p1, "发件人不得出现在审查意见中"
        assert "lisi@example.com" not in p1
        assert "【邮件标题】" not in p1, "邮件标题不得用作合同名称"
        assert "某附件名" not in p1, "附件文件名不得用作合同名称"
        os.remove(out)

    def test_missing_fields_render_pending(self, tpl):
        """字段缺失时显示「（待确认）」，且不留占位符。"""
        out = _fill_review_template(
            template_path=tpl, analysis={"doc_type": "合同协议"},
            original_subject="主题", sender="a@b.com",
        )
        assert out
        p1 = Document(out).paragraphs[1].text
        assert not re.search(PLACEHOLDER_RE, p1), p1
        assert p1.count("（待确认）") == 5, p1
        os.remove(out)

    def test_placeholder_values_do_not_shift(self, tpl):
        """占位符按**名称**填充，与出现次序无关——年月不得错位。

        回归：旧实现按「第 N 个 xxx」定位，新模板下输出过
        「2026x年x月x日…合同内容为10事宜。合同价款张三元。」
        """
        out = _fill_review_template(
            template_path=tpl, analysis=ANALYSIS,
            original_subject="主题", sender="张三 <a@b.com>",
        )
        assert out
        p1 = Document(out).paragraphs[1].text
        now = datetime.now()
        assert f"{now.year}年{now.month}月" in p1, p1
        assert "2026x年" not in p1 and "x月" not in p1, p1
        assert "合同内容为采购施工升降机设备事宜" in p1, p1
        assert "合同价款1,041,748元" in p1, p1
        os.remove(out)

    def test_signature_date_detected_by_shape(self, tmp_path):
        """落款日期按**日期样式**定位，不依赖段序——段数变化也正确。"""
        p = tmp_path / "tpl_short.docx"
        d = Document()
        d.add_paragraph("合同审核意见")
        d.add_paragraph("xxxx年x月x日，法律顾问收到【合同甲方】送审的文书。")
        d.add_paragraph("2023年1月16日")
        d.save(str(p))

        out = _fill_review_template(
            template_path=str(p), analysis=ANALYSIS,
            original_subject="主题", sender="a@b.com",
            attachment_filenames=["起诉状.docx"],
        )
        assert out
        paras = Document(out).paragraphs
        assert len(paras) == 3
        _assert_is_today(paras[-1].text)
        # 正文中的 xxxx年x月x日 同样是当天日期，而非只有末段
        _assert_is_today(paras[1].text.split("，")[0])
        os.remove(out)

    def test_blank_signature_slot_filled(self, tmp_path):
        """新模板把落款段留空（无文字、右对齐），应写入当天日期。"""
        p = tmp_path / "tpl_blank.docx"
        d = Document()
        d.add_paragraph("合同审核意见")
        d.add_paragraph("xxxx年x月x日，法律顾问收到【合同甲方】送审的文书。")
        d.add_paragraph("")
        d.add_paragraph("")
        d.save(str(p))

        out = _fill_review_template(
            template_path=str(p), analysis=ANALYSIS, original_subject="主题",
        )
        assert out
        paras = Document(out).paragraphs
        assert any(re.fullmatch(r"\d{4}年\d{2}月\d{2}日", x.text) for x in paras), \
            [x.text for x in paras]
        os.remove(out)

    def test_body_paragraph_not_overwritten_as_date(self, tmp_path):
        """模板无落款段时，不得把正文段落改写成日期。"""
        p = tmp_path / "tpl_nodate.docx"
        d = Document()
        d.add_paragraph("合同审核意见")
        d.add_paragraph("xxxx年x月x日，法律顾问收到【合同甲方】送审的文书。")
        d.add_paragraph("固定措辞：合同文本法律顾问已审核。")
        d.save(str(p))

        out = _fill_review_template(
            template_path=str(p), analysis=ANALYSIS, original_subject="主题",
        )
        assert out
        txt = _text(out)
        assert "固定措辞：合同文本法律顾问已审核。" in txt
        assert len(Document(out).paragraphs) == 3
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
    """针对仓库真实模板的回归：抬头、固定措辞、页眉图章（章）必须保留。

    模板按文书类型名约定式查找：templates/<文书类型>审核意见模板.docx。
    """

    @pytest.mark.parametrize(
        "doc_type,expected_title",
        [
            ("合同协议", "合同审核意见"),
            ("信访件", "信访事项审核意见"),
            ("政府信息公开", "政府信息公开事项审核意见"),
        ],
    )
    def test_real_template_keeps_title_and_seal(self, doc_type, expected_title):
        tpl = _review_template_path(doc_type)
        if tpl is None:
            pytest.skip(f"模板未配备: {doc_type}")

        src = Document(str(tpl))
        out = _fill_review_template(
            template_path=str(tpl), analysis=ANALYSIS,
            original_subject="【邮件标题】某事项的函",
            sender="张三 <a@b.com>",
            attachment_filenames=["某工程施工合同.docx"],
        )
        assert out
        try:
            got = Document(out)
            body = "\n".join(p.text for p in got.paragraphs)
            # 结构一致：只填充，不增删段
            assert len(got.paragraphs) == len(src.paragraphs)
            # 抬头保持模板原文
            assert got.paragraphs[0].text == expected_title
            # 固定措辞保留（各模板原文）
            assert "律师已审核" in body or "法律顾问已审核" in body
            # 无 LLM 正文渗入
            assert "一、基本情况" not in body
            # 无残留占位符
            assert not re.search(PLACEHOLDER_RE, body), body
            assert not re.search(r"x{2,}年x{1,2}月", body), body
            assert "xxx" not in body, body
            # 邮件标题不得渗入
            assert "【邮件标题】" not in body
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
            # 落款日期存在且为当天
            assert any(
                re.fullmatch(r"\d{4}年\d{2}月\d{2}日", p.text or "") for p in got.paragraphs
            ), [p.text for p in got.paragraphs]
        finally:
            os.remove(out)

    @pytest.mark.parametrize(
        "doc_type",
        ["起诉状", "律师函", "通知书", "判决书", "合同协议", "信访件", "政府信息公开"],
    )
    def test_template_lookup_is_strict_by_name(self, doc_type):
        """查找严格按类型名：有模板的类型返回路径，无模板的返回 None（不抛异常）。"""
        from app.services.mail_forwarder import _review_template_path

        p = _review_template_path(doc_type)
        if p is None:
            assert doc_type in ("起诉状", "律师函", "通知书", "判决书"), \
                f"{doc_type} 应有模板却未找到"
        else:
            assert p.name == f"{doc_type}审核意见模板.docx", p.name
            assert p.exists()

    def test_empty_doc_type_returns_none(self):
        from app.services.mail_forwarder import _review_template_path

        assert _review_template_path("") is None
        assert _review_template_path(None) is None


class TestNoLegacyExtractionHelpers:
    """金额/文书名不再由正则从文件名或正文中猜取，改由 LLM 按正文提取。

    回归：遗留的正则兜底会在模型未返回字段时给出**看似合理但可能错误**的值
    （如把附件名当合同名），掩盖真实缺失，故彻底移除。
    """

    @pytest.mark.parametrize(
        "name",
        ["_extract_doc_title", "_strip_amount_phrases", "_extract_amount",
         "_replace_xxx_in_paragraph", "_replace_paragraph_text"],
    )
    def test_legacy_helper_removed(self, name):
        import app.services.mail_forwarder as mf

        assert not hasattr(mf, name), f"{name} 应已移除"