"""原生修订（Track Changes）模块测试。

覆盖：
- OOXML 修订铁律（w:ins/w:del 必须是 w:r 的兄弟节点，不得嵌套）
- 格式保留（页面设置、字体、rPr 继承）
- 显式标记（【新增】/【修改】/【删除】）与文本 diff 的协同
- 降级路径（非 docx、损坏文件、缺失文件）
"""
import os
import re
import sys
import zipfile

import pytest

from app.services import redline
from app.services.redline import (
    _normalize,
    _parse_marked_text,
    _split_target_paragraphs,
    build_redlined_docx,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# ── 构造一个带真实格式的最小 docx 作为测试底版 ──

def _make_minimal_docx(path, paragraphs, font="仿宋_GB2312", size=28):
    """生成一个带中文正文字体与自定义页面设置的 docx。"""
    from docx import Document
    from docx.shared import Pt, Emu

    doc = Document()
    sec = doc.sections[0]
    sec.page_width = Emu(7560310)
    sec.page_height = Emu(10692130)

    for text in paragraphs:
        p = doc.add_paragraph()
        run = p.add_run(text)
        run.font.size = Pt(size / 2)
        # python-docx 不直接支持 eastAsia，这里手工写入
        rpr = run._element.get_or_add_rPr()
        from docx.oxml.ns import qn as dqn
        rfonts = rpr.find(dqn("w:rFonts"))
        if rfonts is None:
            from lxml import etree
            rfonts = etree.SubElement(rpr, dqn("w:rFonts"))
        rfonts.set(dqn("w:eastAsia"), font)
        rfonts.set(dqn("w:ascii"), "Times New Roman")
        rfonts.set(dqn("w:hAnsi"), "Times New Roman")
    doc.save(str(path))
    return str(path)


@pytest.fixture
def base_docx(tmp_path):
    paras = [
        "合作协议",
        "甲方：示例大学",
        "第一条 合作宗旨",
        "双方本着平等互利原则开展合作。",
        "第二条 合作内容",
        "1.课程共建",
        "2.师资交流",
        "第三条 经费",
        "经费由双方另行约定。",
    ]
    return _make_minimal_docx(tmp_path / "base.docx", paras)


def _read_xml(docx_path):
    with zipfile.ZipFile(docx_path) as z:
        return (z.read("word/document.xml").decode("utf-8"),
                z.read("word/settings.xml").decode("utf-8"))


# ── 标记解析 ──

class TestParseMarkedText:
    def test_plain_text_is_normal(self):
        segs = _parse_marked_text("普通文本")
        assert segs == [("normal", "普通文本")]

    def test_add_marker(self):
        segs = _parse_marked_text("前【新增】新内容【/新增】后")
        kinds = [k for k, _ in segs]
        assert "add" in kinds
        added = "".join(t for k, t in segs if k == "add")
        assert added == "新内容"

    def test_all_marker_types(self):
        text = "【新增】A【/新增】【修改】B【/修改】【删除】C【/删除】"
        segs = _parse_marked_text(text)
        got = {k: "".join(t for kk, t in segs if kk == k)
               for k in ("add", "modify", "delete")}
        assert got == {"add": "A", "modify": "B", "delete": "C"}

    def test_unclosed_marker_does_not_crash(self):
        segs = _parse_marked_text("【新增】没有闭合")
        assert any(k == "add" for k, _ in segs)

    def test_adjacent_same_kind_merged(self):
        segs = _parse_marked_text("【新增】A【/新增】【新增】B【/新增】")
        adds = [t for k, t in segs if k == "add"]
        assert len(adds) == 1
        assert adds[0] == "AB"


class TestSplitTargetParagraphs:
    def test_blank_line_splits(self):
        parts = _split_target_paragraphs("第一段\n\n第二段")
        assert len(parts) == 2

    def test_single_newline_kept(self):
        """单换行是段内软换行，不能拆段（原文书常见「甲方：…\\n乙方：…」）。"""
        parts = _split_target_paragraphs("甲方：A\n乙方：B")
        assert len(parts) == 1

    def test_crlf_normalized(self):
        parts = _split_target_paragraphs("A\r\n\r\nB")
        assert len(parts) == 2


# ── 核心：修订注入 ──

class TestBuildRedlinedDocx:
    def test_insert_produces_w_ins(self, base_docx):
        revised = (
            "合作协议\n\n甲方：示例大学\n\n第一条 合作宗旨\n\n"
            "双方本着平等互利原则开展合作。\n\n"
            "【新增】第一条之一 补充约定：每学期评估一次。【/新增】\n\n"
            "第二条 合作内容"
        )
        out = build_redlined_docx(base_docx, revised)
        assert out and os.path.exists(out)
        x, st = _read_xml(out)
        assert "<w:ins " in x
        assert "<w:trackChanges" in st
        os.remove(out)

    def test_delete_produces_w_del_text(self, base_docx):
        revised = (
            "合作协议\n\n甲方：示例大学\n\n第一条 合作宗旨\n\n"
            "双方本着平等互利原则开展合作。\n\n"
            "第二条 合作内容\n\n【删除】1.课程共建【/删除】\n\n2.师资交流"
        )
        out = build_redlined_docx(base_docx, revised)
        assert out
        x, _ = _read_xml(out)
        del_text = "".join(re.findall(r"<w:delText[^>]*>([^<]*)</w:delText>", x))
        assert "课程共建" in del_text
        os.remove(out)

    def test_marked_delete_not_swallowed_by_equal(self, base_docx):
        """回归：被【删除】标记的段落文本与原文相同，纯 diff 会判为未改动。"""
        revised = (
            "合作协议\n\n甲方：示例大学\n\n第一条 合作宗旨\n\n"
            "双方本着平等互利原则开展合作。\n\n"
            "第二条 合作内容\n\n【删除】2.师资交流【/删除】\n\n第三条 经费"
        )
        out = build_redlined_docx(base_docx, revised)
        assert out
        x, _ = _read_xml(out)
        del_text = "".join(re.findall(r"<w:delText[^>]*>([^<]*)</w:delText>", x))
        assert "师资交流" in del_text, "显式删除标记必须生效"
        os.remove(out)

    def test_no_nested_revision_elements(self, base_docx):
        """铁律：w:ins/w:del 不得出现在 w:r 或 w:t 内部。"""
        revised = (
            "合作协议\n\n【修改】甲方：示例大学（修订）【/修改】\n\n"
            "【新增】新增段落【/新增】\n\n【删除】第三条 经费【/删除】"
        )
        out = build_redlined_docx(base_docx, revised)
        assert out
        x, _ = _read_xml(out)
        bad_run = re.findall(r"<w:r\b[^>]*>(?:(?!</w:r>).)*?<w:(?:ins|del)\b", x, re.S)
        bad_text = re.findall(r"<w:t\b[^>]*>[^<]*<w:(?:ins|del)\b", x)
        assert bad_run == []
        assert bad_text == []
        os.remove(out)

    def test_inserted_run_inherits_original_font(self, base_docx):
        """格式保留：新增 run 必须继承原文的 eastAsia 字体。"""
        revised = (
            "合作协议\n\n【修改】甲方：示例大学（修订）【/修改】\n\n"
            "第一条 合作宗旨\n\n【新增】补充条款。【/新增】"
        )
        out = build_redlined_docx(base_docx, revised)
        assert out
        x, _ = _read_xml(out)
        ins_blocks = re.findall(r"<w:ins\b.*?</w:ins>", x, re.S)
        assert ins_blocks
        for b in ins_blocks:
            assert "eastAsia" in b, f"新增 run 未继承字体: {b[:200]}"
        os.remove(out)

    def test_page_setup_preserved(self, base_docx):
        from docx import Document
        out = build_redlined_docx(base_docx, "合作协议\n\n【新增】新段【/新增】")
        assert out
        d0, d1 = Document(base_docx), Document(out)
        assert d0.sections[0].page_width == d1.sections[0].page_width
        assert d0.sections[0].page_height == d1.sections[0].page_height
        os.remove(out)

    def test_original_fonts_all_present(self, base_docx):
        out = build_redlined_docx(base_docx, "合作协议\n\n【新增】新段【/新增】")
        assert out
        x, _ = _read_xml(out)
        assert "仿宋_GB2312" in x
        assert "Times New Roman" in x
        os.remove(out)

    def test_paragraph_count_not_reduced(self, base_docx):
        """原文段落不得丢失（只增不减，除非显式删除）。"""
        from docx import Document
        n0 = len(Document(base_docx).paragraphs)
        out = build_redlined_docx(base_docx, "合作协议\n\n第二条 合作内容")
        assert out
        n1 = len(Document(out).paragraphs)
        assert n1 >= n0 - 1, f"段落数异常减少: {n0} → {n1}"
        os.remove(out)

    def test_unchanged_text_produces_no_revisions(self, base_docx):
        """全文无改动时不应产生任何修订标记。"""
        from docx import Document
        same = "\n\n".join(p.text for p in Document(base_docx).paragraphs if p.text.strip())
        out = build_redlined_docx(base_docx, same)
        assert out
        x, _ = _read_xml(out)
        assert x.count("<w:ins ") == 0
        assert x.count("<w:del ") == 0
        os.remove(out)

    def test_empty_revised_text(self, base_docx):
        out = build_redlined_docx(base_docx, "")
        # 空文本不应崩溃；返回 None 或产物均可接受
        if out:
            assert os.path.exists(out)
            os.remove(out)

    def test_output_is_valid_zip_with_required_parts(self, base_docx):
        out = build_redlined_docx(base_docx, "合作协议\n\n【新增】X【/新增】")
        assert out
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            assert "word/document.xml" in names
            assert "word/settings.xml" in names
            assert "[Content_Types].xml" in names
            assert z.testzip() is None
        os.remove(out)


# ── 降级路径 ──

class TestFallbackPaths:
    def test_nonexistent_file(self, tmp_path):
        assert build_redlined_docx(str(tmp_path / "nope.docx"), "x") is None

    def test_non_docx_extension(self, tmp_path):
        p = tmp_path / "a.pdf"
        p.write_bytes(b"%PDF-1.4")
        assert build_redlined_docx(str(p), "x") is None

    def test_corrupt_docx_returns_none(self, tmp_path):
        p = tmp_path / "bad.docx"
        p.write_bytes(b"not a zip at all")
        assert build_redlined_docx(str(p), "x") is None

    def test_docx_without_document_xml(self, tmp_path):
        p = tmp_path / "empty.docx"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("foo.txt", "bar")
        assert build_redlined_docx(str(p), "x") is None


# ── 辅助函数 ──

class TestHelpers:
    def test_normalize_strips_all_whitespace(self):
        assert _normalize("甲 方：\u3000A\n B") == "甲方：AB"

    def test_normalize_handles_none(self):
        assert _normalize(None) == ""

    def test_find_soffice_returns_str_or_none(self):
        r = redline.find_soffice()
        assert r is None or isinstance(r, str)

    def test_qn_rejects_non_w_prefix(self):
        with pytest.raises(ValueError):
            redline._qn("a:foo")