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

    def test_single_newline_split_when_lines_look_like_paragraphs(self):
        """「甲方：A / 乙方：B」型行首标签的单换行要拆段。

        回归背景：原先只按空行拆段，LLM 用单个换行分隔段落时（这是常态），
        目标段数与原文段数对不上，段落级对齐只能退化为「整段删除 + 整段新增」，
        封面会被整块划掉后又重复插入一份。
        """
        parts = _split_target_paragraphs("甲方：A\n乙方：B")
        assert parts == ["甲方：A", "乙方：B"]

    def test_single_newline_kept_for_continuation_line(self):
        """续行（不以标签/句末标点开头、且是长句的一部分）仍保留为段内软换行。"""
        text = "双方本着平等互利原则开展合作，共同推进\n人才培养与科研协同等各项工作落地。"
        parts = _split_target_paragraphs(text)
        assert len(parts) == 1
        assert "\n" in parts[0]

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


# ── 段落内容的非 run 元素：超链接 / 制表位 ──

def _add_hyperlink(paragraph, url, text):
    """向段落追加一个真实超链接（w:hyperlink 包裹 w:r）"""
    from docx.oxml.ns import qn as dqn
    from lxml import etree

    r_id = paragraph.part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    hl = etree.SubElement(paragraph._element, dqn("w:hyperlink"))
    hl.set(dqn("r:id"), r_id)
    run = etree.SubElement(hl, dqn("w:r"))
    t = etree.SubElement(run, dqn("w:t"))
    t.text = text
    return hl


def _paragraph_kinds(body):
    """返回 [(顶层段落文本, 是否含 w:del, 是否含 w:ins)]，按文档顺序"""
    from lxml import etree
    xml = etree.fromstring(body.encode("utf-8"))
    out = []
    for p in xml.iter("{%s}p" % W_NS):
        texts = "".join(t.text or "" for t in p.iter("{%s}t" % W_NS))
        dels = "".join(t.text or "" for t in p.iter("{%s}delText" % W_NS))
        out.append((texts, dels,
                    p.find("{%s}del" % W_NS) is not None,
                    p.find("{%s}ins" % W_NS) is not None))
    return out


def _make_docx(path, builder):
    from docx import Document
    doc = Document()
    builder(doc)
    doc.save(str(path))
    return str(path)


class TestNonRunContentDeletion:
    """删除段落时必须覆盖嵌套容器内的 run 与制表位

    回归背景：_delete_paragraph 原先只处理 <w:p> 的直接子 <w:r>，
    超链接（w:hyperlink 内）的文字不会被包进 <w:del>，却仍返回 True，
    表现为「模型要求删除该段、系统报告已应用，链接文字仍留在正文且无修订标记」。
    """

    def test_hyperlink_text_wrapped_in_del(self, tmp_path):
        def build(doc):
            doc.add_paragraph("前言")
            p = doc.add_paragraph()
            p.add_run("第1行：")
            _add_hyperlink(p, "https://example.com/x", "请以链接内容为准")
            doc.add_paragraph("结尾")

            from docx.oxml.ns import qn as dqn
            from lxml import etree
            run = p.add_run("")
            etree.SubElement(run._element, dqn("w:br"))

        src = _make_docx(tmp_path / "link.docx", build)
        out = build_redlined_docx(src, "前言\n\n结尾")
        assert out
        body, _ = _read_xml(out)

        # 链接文字必须落在 w:delText 中，且不再出现在 w:t 中
        assert "请以链接内容为准" in body.split("<w:del")[1] or "请以链接内容为准" in body
        text_nodes = re.findall(r"<w:t[^>]*>([^<]*)</w:t>", body)
        assert "请以链接内容为准" not in text_nodes
        del_text_nodes = re.findall(r"<w:delText[^>]*>([^<]*)</w:delText>", body)
        assert "请以链接内容为准" in "".join(del_text_nodes)
        os.remove(out)

    def test_tab_wrapped_in_del(self, tmp_path):
        def build(doc):
            doc.add_paragraph("甲方：")
            p = doc.add_paragraph()
            r1 = p.add_run("甲方")
            from docx.oxml.ns import qn as dqn
            from lxml import etree
            etree.SubElement(r1._element, dqn("w:tab"))
            p.add_run("乙方")
            # 纯换行 run（无文本），删除时同样不能把这些元素留在 w:del 之外
            r2 = p.add_run("")
            etree.SubElement(r2._element, dqn("w:br"))
            doc.add_paragraph("丙方：")

        src = _make_docx(tmp_path / "tab.docx", build)
        out = build_redlined_docx(src, "甲方：\n\n丙方：")
        assert out
        body, _ = _read_xml(out)

        # 残留的 <w:tab/> / <w:br/> 不得出现在任何 w:del 之外
        outside = re.sub(r"<w:del [^>]*>.*?</w:del>", "", body, flags=re.S)
        assert "<w:tab/>" not in outside
        assert "<w:br/>" not in outside
        del_text_nodes = "".join(re.findall(r"<w:delText[^>]*>([^<]*)</w:delText>", body))
        assert "甲方" in del_text_nodes and "乙方" in del_text_nodes
        os.remove(out)


# ── 插入位置 ──

class TestInsertPosition:
    def test_insert_at_document_start_lands_before_first_paragraph(self, base_docx):
        """在正文最前面新增一段，必须出现在第一段之前

        回归背景：插入点在最前面时没有「前一段」可作锚点，原实现会退化到
        「插到第一段之后」，把前置条款挪到错误位置。
        """
        revised = ("【新增】本补充协议自双方签署之日起生效。【/新增】\n\n"
                   "合作协议\n\n甲方：示例大学")
        out = build_redlined_docx(base_docx, revised)
        assert out
        body, _ = _read_xml(out)
        paras = _paragraph_kinds(body)
        non_empty = [p for p in paras if p[0] or p[1]]
        assert non_empty, "应至少产出若干段落"

        all_text = "\n".join(p[0] for p in paras)
        assert "本补充协议自双方签署之日起生效。" in all_text, \
            f"新增段落被静默丢弃，实际全文：{all_text!r}"

        first_text = non_empty[0][0]
        assert "本补充协议自双方签署之日起生效。" in first_text, \
            f"新增段落未落在最前面，实际首段为：{first_text!r}"
        assert non_empty[0][3] is True, "最前面那段应带 w:ins 标记"
        os.remove(out)

    def test_insert_in_middle_still_correct(self, tmp_path):
        """中间插入不受影响（防止修正文首时改坏常规路径）"""
        paras = ["第一条 甲", "第二条 乙", "第三条 丙"]
        src = _make_minimal_docx(tmp_path / "mid.docx", paras)
        revised = ("第一条 甲\n\n【新增】第二条之一 补充约定【/新增】\n\n"
                   "第二条 乙\n\n第三条 丙")
        out = build_redlined_docx(src, revised)
        assert out
        body, _ = _read_xml(out)
        texts = [p[0] for p in _paragraph_kinds(body) if p[0]]
        assert texts.index("第二条之一 补充约定") == texts.index("第一条 甲") + 1
        os.remove(out)


# ── 修订颗粒度（段落内字符级对齐） ──

class TestInlineGranularity:
    """回归背景：旧实现是**纯段落级** diff，整段只要有一个字不同就整段
    `<w:del>` + 整段 `<w:ins>`，审阅者看不出到底改了哪个词。
    现在段落内做字符级对齐，只标注真正改动的词句。
    """

    def _revision_fragments(self, docx_path):
        """返回 [(kind, text)]，kind ∈ {ins, del}，按文档顺序"""
        body, _ = _read_xml(docx_path)
        frags = []
        for m in re.finditer(r"<w:(ins|del)\b.*?</w:\1>", body, re.S):
            text = "".join(re.findall(r"<w:(?:t|delText)[^>]*>([^<]*)</w:", m.group(0)))
            frags.append((m.group(1), text))
        return frags

    def test_word_level_change_marks_only_the_word(self, tmp_path):
        """只改一个日期词：标记应只圈住该词，其余原文保持未标记。"""
        src = _make_minimal_docx(
            tmp_path / "gran.docx",
            ["第一条 甲方应在30日内支付全部价款。", "第二条 乙方应交付货物。"],
        )
        revised = "第一条 甲方应在60日内支付全部价款。\n\n第二条 乙方应交付货物。"
        out = build_redlined_docx(src, revised)
        assert out
        frags = self._revision_fragments(out)
        combined = "".join(t for _, t in frags)
        # 整个段落不应被标为修订
        assert "甲方应在30日内支付全部价款" not in combined
        # 只有变化的那几个字符
        assert any(t == "3" for k, t in frags if k == "del"), frags
        assert any(t == "6" for k, t in frags if k == "ins"), frags
        os.remove(out)

    def test_unchanged_paragraph_not_marked(self, tmp_path):
        """未改动的段落不得出现任何修订标记。"""
        src = _make_minimal_docx(
            tmp_path / "gran2.docx",
            ["第一条 甲方应在30日内支付全部价款。", "第二条 乙方应交付货物。"],
        )
        revised = "第一条 甲方应在60日内支付全部价款。\n\n第二条 乙方应交付货物。"
        out = build_redlined_docx(src, revised)
        assert out
        body, _ = _read_xml(out)
        for text, dels, has_del, has_ins in _paragraph_kinds(body):
            if "乙方应交付货物" in text:
                assert not has_del and not has_ins, \
                    f"未改动段落被误标修订：{text!r}"
        os.remove(out)

    def test_paragraph_count_unchanged_for_inline_edit(self, tmp_path):
        """段落内细化不增删段落（区别于整段替换会多出一段）。"""
        src = _make_minimal_docx(
            tmp_path / "gran3.docx",
            ["第一条 甲方应在30日内支付全部价款。", "第二条 乙方应交付货物。"],
        )
        revised = "第一条 甲方应在60日内支付全部价款。\n\n第二条 乙方应交付货物。"
        out = build_redlined_docx(src, revised)
        assert out
        body, _ = _read_xml(out)
        texts = [t for t, d, hd, hi in _paragraph_kinds(body) if t or d]
        assert len(texts) == 2, f"段落数应保持不变，实际 {texts!r}"
        os.remove(out)

    def test_symmetric_text_has_no_false_change(self, tmp_path):
        """提交前修订（调换字序）也不应把整段标红。"""
        src = _make_minimal_docx(
            tmp_path / "gran4.docx", ["甲方应于收到货物后支付价款。"]
        )
        revised = "甲方应于支付货物后收到价款。"
        out = build_redlined_docx(src, revised)
        assert out
        frags = self._revision_fragments(out)
        combined = "".join(t for _, t in frags)
        assert len(combined) < 12, f"改动占比不大，不应大段标记：{frags!r}"
        os.remove(out)

    def test_large_rewrite_falls_back_to_whole_paragraph(self, tmp_path):
        """差异超阈值时回退为整段替换（避免碎片化标记）。"""
        src = _make_minimal_docx(
            tmp_path / "gran5.docx", ["第一条 甲方应在30日内支付全部价款。"]
        )
        revised = "第一条 乙方有权单方解除本协议并要求甲方承担全部损失。"
        out = build_redlined_docx(src, revised)
        assert out
        frags = self._revision_fragments(out)
        del_text = "".join(t for k, t in frags if k == "del")
        # 回退路径 → 原文整段进 w:del
        assert "甲方应在30日内支付全部价款" in del_text, frags
        os.remove(out)

    def test_inline_keeps_no_nested_revision_elements(self, tmp_path):
        """铁律回归：细粒度标记仍不得嵌套进 w:r / w:t。"""
        src = _make_minimal_docx(
            tmp_path / "gran6.docx", ["第一条 甲方应在30日内支付全部价款。"]
        )
        out = build_redlined_docx(src, "第一条 甲方应在60日内支付全部价款。")
        assert out
        body, _ = _read_xml(out)
        assert re.findall(r"<w:r\b[^>]*>(?:(?!</w:r>).)*?<w:(?:ins|del)\b", body, re.S) == []
        assert re.findall(r"<w:t\b[^>]*>[^<]*<w:(?:ins|del)\b", body) == []
        os.remove(out)

    def test_inline_inserted_run_inherits_font(self, tmp_path):
        """细粒度新增的 run 必须继承原文字体。"""
        src = _make_minimal_docx(
            tmp_path / "gran7.docx", ["第一条 甲方应在30日内支付全部价款。"]
        )
        out = build_redlined_docx(src, "第一条 甲方应在60日内支付全部价款。")
        assert out
        body, _ = _read_xml(out)
        ins_blocks = re.findall(r"<w:ins\b.*?</w:ins>", body, re.S)
        assert ins_blocks
        for b in ins_blocks:
            assert "eastAsia" in b, f"细粒度新增未继承字体: {b[:200]}"
        os.remove(out)

    def test_inline_diff_pieces_are_ordered(self, tmp_path):
        """del/ins 片段顺序必须能正确重建新文（插入在删除之后）。"""
        src = _make_minimal_docx(
            tmp_path / "gran8.docx", ["第一条 甲方应在30日内支付全部价款。"]
        )
        out = build_redlined_docx(src, "第一条 甲方应在60日内支付全部价款。")
        assert out
        body, _ = _read_xml(out)
        # 定位目标段落，按子节点顺序还原「接受全部修订后」的文本
        from lxml import etree
        xml = etree.fromstring(body.encode("utf-8"))
        for p in xml.iter("{%s}p" % W_NS):
            text = "".join(t.text or "" for t in p.iter("{%s}t" % W_NS))
            if "甲方应在" in text:
                # 接受修订后的文本 = 保留 w:t（含 ins 内），去掉 delText
                accepted = "".join(
                    t.text or "" for t in p.iter("{%s}t" % W_NS)
                )
                assert accepted == "第一条 甲方应在60日内支付全部价款。", accepted
                deltext = "".join(
                    t.text or "" for t in p.iter("{%s}delText" % W_NS)
                )
                assert deltext == "3", deltext
                break
        else:
            pytest.fail("未找到目标段落")
        os.remove(out)


    def test_mixed_span_inlines_edits_and_appends_new_paragraph(self, tmp_path):
        """回归：区间内「改动段落 + 末尾新增段落」混合时，改动部分仍须词级细化。

        回归背景：LCS 会把「末尾追加一段」与它前面的几处改动合并成同一个
        replace 区间（原文 3 段 ↔ 目标 4 段）。早期实现要求两侧段数严格相等，
        于是一处追加就把前面所有段落的词级细化全部放弃，退化成整段删+整段加。
        """
        src = _make_minimal_docx(
            tmp_path / "mixed.docx",
            ["第一条 甲方应在30日内支付价款。", "第二条 乙方应交付货物。"],
        )
        revised = (
            "第一条 甲方应在60日内支付价款。\n\n"
            "第二条 乙方应交付货物。\n\n"
            "【新增】第三条 逾期付款应按日计息。【/新增】"
        )
        out = build_redlined_docx(src, revised)
        assert out
        frags = self._revision_fragments(out)
        del_text = "".join(t for k, t in frags if k == "del")
        # 改动段落只应标记变化的那一个字，而不是整段
        assert del_text.strip() == "3", f"改动段落未被词级细化：{frags!r}"
        # 新增段落仍作为独立的插入段落出现
        ins_text = "".join(t for k, t in frags if k == "ins")
        assert "第三条 逾期付款应按日计息。" in ins_text, frags
        os.remove(out)

    def test_no_duplicate_paragraph_when_inlining(self, tmp_path):
        """回归：词级细化后不得把同一段落再作为整段新增插入（重复内容）。"""
        src = _make_minimal_docx(
            tmp_path / "dup.docx",
            ["第七条 争议由本院管辖。", "第八条 未尽事宜另行协商。"],
        )
        revised = (
            "第七条 争议由工程所在地法院管辖。\n\n"
            "第八条 未尽事宜另行协商。"
        )
        out = build_redlined_docx(src, revised)
        assert out
        body, _ = _read_xml(out)
        from lxml import etree
        accepted = "".join(
            t.text or ""
            for t in etree.fromstring(body.encode("utf-8")).iter("{%s}t" % W_NS)
        )
        assert accepted.count("第八条 未尽事宜另行协商。") == 1, \
            f"段落被重复插入：{accepted!r}"
        os.remove(out)


class TestInlineDiffRuns:
    """`_inline_diff_runs` 单元行为"""

    def test_equal_returns_none(self):
        assert redline._inline_diff_runs("同样文本", "同样文本") is None

    def test_empty_side_returns_none(self):
        assert redline._inline_diff_runs("", "新文") is None
        assert redline._inline_diff_runs("旧文", "") is None

    def test_small_change_yields_pieces(self):
        pieces = redline._inline_diff_runs("甲方应在30日内支付。", "甲方应在60日内支付。")
        assert pieces is not None
        kinds = [k for k, _ in pieces]
        assert "del" in kinds and "ins" in kinds
        assert "".join(t for k, t in pieces if k != "del") == "甲方应在60日内支付。"

    def test_large_rewrite_returns_none(self):
        """差异超阈值 → 回退整段（返回 None）。"""
        old = "第一条 甲方应在30日内支付全部价款。"
        new = "第一条 乙方有权单方解除本协议并要求甲方承担全部损失。"
        assert redline._inline_diff_runs(old, new) is None

    def test_pieces_merge_adjacent_kinds(self):
        """相邻同类片段应合并，避免产生过多修订条目。"""
        pieces = redline._inline_diff_runs("abcdef", "abXYef")
        kinds = [k for k, _ in pieces]
        assert kinds == ["equal", "del", "ins", "equal"], pieces
        assert "".join(t for k, t in pieces if k != "del") == "abXYef"
        assert "".join(t for k, t in pieces if k != "ins") == "abcdef"


class TestInlineFallbackGuards:
    """细化路径的守卫：结构不匹配时必须回退，不得误改原文。"""

    def test_paragraph_with_hyperlink_not_inlined(self, tmp_path):
        """段落含超链接等嵌套结构时回退整段替换，避免连带删掉链接。"""
        def build(doc):
            p = doc.add_paragraph()
            p.add_run("详见")
            _add_hyperlink(p, "https://example.com/a", "此处链接")
            p.add_run("说明。")

        src = _make_docx(tmp_path / "guard.docx", build)
        out = build_redlined_docx(src, "详见此处链接说明（修订）。")
        assert out
        body, _ = _read_xml(out)
        # 不得出现「链接被静默移除」：原文段落整体进 del 或保持可追溯
        assert "此处链接" in "".join(
            re.findall(r"<w:(?:t|delText)[^>]*>([^<]*)</w:", body)
        )
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


# ── 回归：封面重复插入 / 软换行 / 兜底自检 ──
#
# 背景：客户反馈两份真实文书「修订部分格式混乱」。定位到三个缺陷：
# A. 标记块内的单个换行被当成段内软换行，导致目标段数与原文段数对不上，
#    封面被整块划掉后又重复插入一份；
# B. 新增段落用 <w:br/> 表示换行而非真实 <w:p>，段落级格式（居中/缩进）
#    作用于整块，版式与原文不一致；
# D. 对齐失败时没有任何兜底，直接产出「整篇划掉」的文档。
# 以下用例锁定这三类问题的修复。

class TestCoverPageRegression:
    """封面型结构：多个短行标题 + 字段行，必须拆成独立段落处理。"""

    COVER = [
        "天津市",
        "小型建设工程施工合同",
        "（JF-2001-015）",
        "工程名称：某小区外墙维修工程",
        "工程编号：260929106560",
        "建设单位：某物业管理有限公司",
        "施工单位：某建筑工程有限公司",
        "签订日期：    年   月   日",
    ]

    def test_cover_lines_split_into_paragraphs(self):
        """封面各行必须以单换行拆成独立段落，而不是压成一段。"""
        block = "\n".join(self.COVER)
        parts = _split_target_paragraphs(block)
        assert len(parts) == len(self.COVER)
        assert parts[0] == "天津市"
        assert parts[2] == "（JF-2001-015）"

    def test_modified_cover_is_not_delete_plus_insert(self, tmp_path):
        """改动封面个别字时，应就地细化，不能整块删除 + 整块新增。

        回归背景：正是这个缺陷让封面在 Word 里被全部划掉后重复一份。
        """
        src = _make_minimal_docx(tmp_path / "cover.docx", self.COVER)
        # 只把「外墙外墙」笔误改掉、补全日期
        revised = "\n".join([
            "天津市",
            "小型建设工程施工合同",
            "（JF-2001-015）",
            "工程名称：某小区外墙维修工程",
            "工程编号：260929106560",
            "建设单位：某物业管理有限公司",
            "施工单位：某建筑工程有限公司",
            "签订日期：2026年12月31日",
        ])
        out = build_redlined_docx(src, revised)
        assert out
        body, _ = _read_xml(out)
        # 未改动的封面行不得出现在删除内容里
        del_text = "".join(re.findall(r"<w:delText[^>]*>([^<]*)</w:delText>", body))
        assert "天津市" not in del_text
        assert "小型建设工程施工合同" not in del_text
        assert "JF-2001-015" not in del_text
        # 也不得产生整段重复插入
        ins_text = _inserted_text(body)
        assert ins_text.count("小型建设工程施工合同") == 0
        os.remove(out)

    def test_accepted_text_equals_revised(self, tmp_path):
        """接受全部修订后，正文段落应等于 LLM 给出的修订文本。"""
        src = _make_minimal_docx(tmp_path / "cover2.docx", self.COVER)
        revised_cover = [
            "天津市",
            "小型建设工程施工合同",
            "（JF-2001-015）",
            "工程名称：某小区外墙维修工程",
            "工程编号：260929106560",
            "建设单位：某物业管理有限公司",
            "施工单位：某建筑工程有限公司",
            "签订日期：2026年12月31日",
        ]
        out = build_redlined_docx(src, "\n".join(revised_cover))
        assert out
        body, _ = _read_xml(out)
        accepted = _accepted_paragraph_texts(body)
        for line in revised_cover:
            assert line in accepted, f"接受修订后缺少: {line}"
        os.remove(out)


class TestNoSoftBreakInInsertions:
    """新增内容必须用真实 <w:p> 分段，不得出现「<w:ins> 内的 <w:br/>」。"""

    def test_inserted_paragraphs_use_real_breaks(self, tmp_path):
        src = _make_minimal_docx(tmp_path / "base.docx", ["合作协议", "第一条 合作宗旨"])
        # 一条新增内容内部换行 → 必须产生两个独立段落
        revised = "合作协议\n\n第一条 合作宗旨\n\n【新增】新增第一句。\n新增第二句。【/新增】"
        out = build_redlined_docx(src, revised)
        assert out
        body, _ = _read_xml(out)
        assert "<w:br" not in body, "新增内容不应使用 <w:br/> 软换行"
        ins_text = _inserted_text(body)
        assert "新增第一句。" in ins_text
        assert "新增第二句。" in ins_text
        os.remove(out)

    def test_multi_line_insert_produces_multiple_paragraphs(self, tmp_path):
        src = _make_minimal_docx(tmp_path / "base2.docx", ["标题"])
        revised = "标题\n\n【新增】甲\n乙\n丙【/新增】"
        out = build_redlined_docx(src, revised)
        assert out
        body, _ = _read_xml(out)
        # 三个独立 <w:ins>（各自在独立段落里）
        assert body.count("<w:ins ") >= 3
        os.remove(out)

    def test_inline_path_newline_becomes_real_paragraph(self, tmp_path):
        """段落内细化时，ins 片段里的换行也必须产生真实段落。

        回归背景：`_replace_paragraph_inline` 原先直接把含 `\\n` 的片段交给
        `_set_run_text`，结果在 <w:ins> 里写出 <w:br/> 软换行 —— 段落级格式
        会作用于整块，版式与原文不一致。这类换行出现在「原文一行、修订后
        在中间另起一段」的场景。
        """
        src = _make_minimal_docx(
            tmp_path / "inline.docx",
            ["12.1 协商不成时，可按下列第 2 种方式解决：", "结尾段落。"],
        )
        revised = (
            "12.1 协商不成时，可按下列第 2 种方式解决：\n"
            "①向仲裁委员会申请仲裁；\n"
            "②向人民法院起诉。\n\n结尾段落。"
        )
        out = build_redlined_docx(src, revised)
        assert out
        body, _ = _read_xml(out)
        assert "<w:br" not in body, "段落内细化不应产出 <w:br/> 软换行"
        accepted = _accepted_paragraph_texts(body)
        assert "①向仲裁委员会申请仲裁；" in accepted
        assert "②向人民法院起诉。" in accepted
        os.remove(out)


class TestDeletedRatioGuard:
    """兜底自检：对齐彻底失败时应放弃生成，而不是产出整篇划掉的文档。"""

    def test_ratio_helper_measures_deleted_share(self):
        from lxml import etree
        xml = (
            '<w:body xmlns:w="%s">'
            '<w:p><w:r><w:t>保留</w:t></w:r></w:p>'
            '<w:p><w:del w:id="1" w:author="a"><w:r><w:delText>删掉的文字</w:delText></w:r></w:del></w:p>'
            '</w:body>' % W_NS
        )
        body = etree.fromstring(xml)
        deleted, ratio = redline._deleted_ratio(body)
        assert deleted == 5
        assert ratio == pytest.approx(5 / 7)

    def test_ratio_helper_handles_empty_body(self):
        from lxml import etree
        body = etree.fromstring('<w:body xmlns:w="%s"/>' % W_NS)
        assert redline._deleted_ratio(body) == (0, 0.0)

    def test_alignment_failure_is_rejected(self, tmp_path):
        """构造一个「除第一段外全部改掉」的大文档，应触发兜底放弃。

        这里直接验证阈值语义：删除量足够大且占比过高 → build 返回 None。
        """
        paras = ["第一条 原始条款"] + [f"原始段落内容第{i}项，描述相关事项。" for i in range(60)]
        src = _make_minimal_docx(tmp_path / "big.docx", paras)
        # 目标与原文几乎完全不重合 → 对齐必然失败
        revised = "\n\n".join(["第一条 原始条款"] +
                              [f"完全不同的全新条款第{i}项，另行约定事项。" for i in range(60)])
        out = build_redlined_docx(src, revised)
        # 兜底生效时返回 None；若对齐足够聪明没有触发，则必须满足「删除占比 < 阈值」
        if out is not None:
            body, _ = _read_xml(out)
            from lxml import etree
            root = etree.fromstring(body.encode("utf-8"))
            _, ratio = redline._deleted_ratio(root)
            assert ratio <= redline._MAX_DELETED_RATIO
            os.remove(out)


class TestInlineOneToMany:
    """一段被拆成多段 / 多段并成一段时的细化（修复 C）。"""

    def test_one_paragraph_split_into_two(self, tmp_path):
        """原文一段含软换行，改写后拆成两个独立段落 → 应细化而非整段删增。"""
        def build(doc):
            p = doc.add_paragraph()
            r = p.add_run("甲方：某投资公司")
            from docx.oxml.ns import qn as dqn
            from lxml import etree
            etree.SubElement(r._element, dqn("w:br"))
            p.add_run("乙方：某建设公司")
            doc.add_paragraph("结尾段落。")

        src = _make_docx(tmp_path / "soft.docx", build)
        revised = "甲方：某投资公司\n\n乙方：某建设公司\n\n结尾段落。"
        out = build_redlined_docx(src, revised)
        assert out
        body, _ = _read_xml(out)
        # 两方名称都未改动 → 不应出现在删除内容里
        del_text = "".join(re.findall(r"<w:delText[^>]*>([^<]*)</w:delText>", body))
        assert "某投资公司" not in del_text
        assert "某建设公司" not in del_text
        os.remove(out)


# ── 测试辅助：从 document.xml 提取接受/插入后的文本 ──

def _inserted_text(body_xml: str) -> str:
    """提取所有 <w:ins> 内的文本（即新增内容）。"""
    out = []
    for m in re.finditer(r"<w:ins [^>]*>(.*?)</w:ins>", body_xml, flags=re.S):
        out.append("".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", m.group(1))))
    return "".join(out)


def _accepted_paragraph_texts(body_xml: str) -> list:
    """按段落提取「接受全部修订后」的文本（保留 w:t，丢弃 w:delText）。"""
    from lxml import etree
    root = etree.fromstring(body_xml.encode("utf-8"))
    qn = redline._qn
    out = []
    for p in root.iter(qn("w:p")):
        parts = []
        for node in p.iter():
            if node.tag == qn("w:t"):
                parts.append(node.text or "")
        txt = "".join(parts).strip()
        if txt:
            out.append(txt)
    return out


# ── C 方案：标记段的「段落回流」 ──

class TestReflowMarkedSegments:
    """标记段必须按**换行**回流成段落，而不是每个标记段各成一段。

    回归背景：`_parse_marked_text` 是按**标记**切分的，不是按段落切分的。
    「只改一句话」会被切成 3 个片段（前文 normal + 改动 modify + 后文 normal）。
    若把每个片段都当成一个目标段落，目标段数就多于原文段数，段落级 LCS
    全部错位，产物里出现「维修工程维修工程」这种重复文本，
    接受/拒绝修订都还原不出正确结果。
    """

    def test_mid_paragraph_marker_stays_one_paragraph(self):
        """标记在段落中间（前后无换行）→ 必须仍是一个段落"""
        from app.services.redline import _parse_marked_text, _reflow_marked_segments
        segs = _parse_marked_text(
            "工程名称：鑫庭花园外墙示范小城镇安置房专项【修改】维修工程【/修改】")
        groups = _reflow_marked_segments(segs)
        assert len(groups) == 1, f"段内标记不应拆段: {groups}"
        assert "".join(t for _, t in groups[0]) == \
            "工程名称：鑫庭花园外墙示范小城镇安置房专项维修工程"

    def test_mid_paragraph_marker_keeps_kinds(self):
        """回流后各片段的标记类型必须保留（供着色/修订类型判定）"""
        from app.services.redline import (
            _parse_marked_text, _reflow_marked_segments, _render_paragraph_group,
        )
        segs = _parse_marked_text("第一条 甲方应在【修改】60【/修改】日内支付。")
        groups = _reflow_marked_segments(segs)
        assert len(groups) == 1
        kinds = [k for k, _ in groups[0]]
        assert "modify" in kinds
        text, kind = _render_paragraph_group(groups[0])
        assert text == "第一条 甲方应在60日内支付。"
        assert kind == "modify"

    def test_trailing_add_stays_one_paragraph(self):
        """段尾追加内容（无换行）→ 仍是一个段落，不应变成两段"""
        from app.services.redline import _parse_marked_text, _reflow_marked_segments
        segs = _parse_marked_text("第二条 工期。【新增】本条经双方确认后生效。【/新增】")
        groups = _reflow_marked_segments(segs)
        assert len(groups) == 1, f"段尾追加不应拆段: {groups}"

    def test_real_newline_splits_paragraphs(self):
        """真实换行分隔的两个段落标题 → 必须拆成两段"""
        from app.services.redline import _parse_marked_text, _reflow_marked_segments
        segs = _parse_marked_text("第一条 工期\n第二条 价款")
        groups = _reflow_marked_segments(segs)
        assert len(groups) == 2, f"换行分隔应拆段: {groups}"

    def test_blank_line_splits_paragraphs(self):
        """空行 → 必定拆段"""
        from app.services.redline import _parse_marked_text, _reflow_marked_segments
        segs = _parse_marked_text("第一条 工期\n\n第二条 价款")
        groups = _reflow_marked_segments(segs)
        assert len(groups) == 2

    def test_soft_newline_is_preserved(self):
        """判定为「不断段」的换行必须作为软换行保留在段内

        不能把 `\\n` 吃掉：下游依赖它决定是否用真实 <w:p> 分段（铁律 4）。
        """
        from app.services.redline import (
            _parse_marked_text, _reflow_marked_segments, _render_paragraph_group,
        )
        segs = _parse_marked_text(
            "12.1 协商不成时，可按下列第 2 种方式解决：\n"
            "①向仲裁委员会申请仲裁；\n"
            "②向人民法院起诉。")
        groups = _reflow_marked_segments(segs)
        text, _ = _render_paragraph_group(groups[0])
        assert "\n" in text, "软换行不应被吞掉"
        assert text.count("\n") == 2

    def test_single_line_group_unchanged(self):
        """单行内容 → 一段，回归最基本情形"""
        from app.services.redline import _parse_marked_text, _reflow_marked_segments
        segs = _parse_marked_text("普通的一段话。")
        groups = _reflow_marked_segments(segs)
        assert len(groups) == 1

    def test_empty_segments_returns_empty(self):
        from app.services.redline import _reflow_marked_segments
        assert _reflow_marked_segments([]) == []


class TestNoDuplicateTextAfterMidParagraphEdit:
    """段内标记不得导致文本重复（C 方案的核心回归）。"""

    def test_mid_paragraph_edit_does_not_duplicate(self, tmp_path):
        """「只改一句话」接受修订后，文本必须与预期完全一致、无重复。"""
        src = _make_minimal_docx(
            tmp_path / "dup.docx",
            ["工程名称：某示范小城镇安置房专项维修工程"],
        )
        revised = "工程名称：某示范小城镇安置房专项【修改】维修工程【/修改】"
        out = build_redlined_docx(src, revised)
        assert out
        body, _ = _read_xml(out)
        accepted = "".join(_accepted_paragraph_texts(body))
        assert accepted == "工程名称：某示范小城镇安置房专项维修工程"
        assert "维修工程维修工程" not in accepted
        os.remove(out)

    def test_trailing_add_does_not_duplicate_paragraph(self, tmp_path):
        """段尾【新增】不得把整段复制一遍（既保留原文又插入一份相同段落）。"""
        src = _make_minimal_docx(
            tmp_path / "tail.docx",
            ["第二条 工期。", "第三条 价款。"],
        )
        revised = ("第二条 工期。【新增】本条经双方确认后生效。【/新增】\n\n"
                   "第三条 价款。")
        out = build_redlined_docx(src, revised)
        assert out
        body, _ = _read_xml(out)
        accepted = _accepted_paragraph_texts(body)
        # 原文段只能出现一次
        assert sum(1 for t in accepted if t.startswith("第二条 工期。")) == 1
        joined = "".join(accepted)
        assert joined.count("第二条 工期。") == 1
        assert "本条经双方确认后生效。" in joined
        os.remove(out)

    def test_paragraph_count_matches_source(self, tmp_path):
        """段内细化的产物段落数必须与原文一致（不得凭空增加段落）。"""
        paras = [f"第{i}条 内容{i}。" for i in range(1, 11)]
        src = _make_minimal_docx(tmp_path / "count.docx", paras)
        # 只在第 3 段中间改一处，其余不变
        revised = "\n\n".join(
            "第3条 【修改】修改后【/修改】内容3。" if p == "第3条 内容3。" else p
            for p in paras
        )
        out = build_redlined_docx(src, revised)
        assert out
        body, _ = _read_xml(out)
        assert len(_accepted_paragraph_texts(body)) == len(paras)
        os.remove(out)