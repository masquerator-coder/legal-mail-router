"""
原生修订（Track Changes）渲染 — 以原文书为底版，保留全部格式。

背景：此前修改版文书用 python-docx 从空白文档重建，原文书的页面设置、
字体（如方正小标宋_GBK）、字号、行距、首行缩进、页眉页脚、表格与图片全部丢失。

本模块改为在**原文书本体**上注入 OOXML 修订标记（<w:ins>/<w:del>），
Word 打开后即为标准的「修订」视图，律师可逐条接受/拒绝，格式完全保持。

OWORD 修订结构铁律（来自 docx skill，违反会导致 Word 显示 0 处修订）：
1. <w:ins>/<w:del> 必须是 <w:p> 的直接子节点（<w:r> 的兄弟），
   放进 <w:r>（尤其 <w:t>）内部会被 Word 完全忽略。
2. 整段替换 run，绝不做字符区间切分 —— 否则标记会落进文本节点内部。
3. 锚点必须落在单个 run 内；跨 run 拼接会把不同位置的 run 搅乱成乱码。
另外必须在 word/settings.xml 写入 <w:trackChanges/>，否则标记只当普通格式显示。

段落结构铁律（违反会导致「格式混乱、修订看不清」）：
4. **新增内容必须用真实 <w:p> 分段，不得用 <w:br/> 软换行。**
   软换行不产生新段落，段落级格式（居中/缩进/间距/编号）会作用于整块，
   版式与原文不符，修订标记也连成一大片无法逐项辨认。
   `_set_run_text` 的换行分支只服务于「删除原文本来就有的软换行」。
5. **标记块内的单个换行要按行文形态判断是否拆段**（见 `_split_target_paragraphs`）。
   LLM 用单个换行分隔段落是常态；若一律当成软换行，目标段数与原文段数对不上，
   段落级对齐只能退化为「整段删除 + 整段新增」，表现为封面被整块划掉后又重复插入。
6. **段落对齐失败必须有兜底**（见 `_deleted_ratio` 与 `_MAX_DELETED_RATIO`）：
   宁可放弃生成修订版，也不产出整篇带删除线的文书。
"""
import copy
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from typing import Optional

logger = logging.getLogger(__name__)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"

# 修订作者名（显示在 Word 的修订气泡里）
REDLINE_AUTHOR = "律师智能审核系统"

# LibreOffice 可执行文件候选路径（用于 .doc → .docx 转换）
_SOFFICE_CANDIDATES = (
    r"C:\Program Files\LibreOffice\program\soffice.com",
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.com",
    "/usr/bin/soffice",
    "/usr/local/bin/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)


def _qn(tag: str) -> str:
    """把 'w:ins' 形式转成 lxml 的 Clark 记法"""
    pfx, local = tag.split(":", 1)
    if pfx != "w":
        raise ValueError(f"仅支持 w: 前缀: {tag}")
    return f"{{{W_NS}}}{local}"


def find_soffice() -> Optional[str]:
    """定位 LibreOffice 可执行文件（用于 .doc → .docx）"""
    for p in _SOFFICE_CANDIDATES:
        if os.path.exists(p):
            return p
    return shutil.which("soffice") or shutil.which("libreoffice")


def convert_doc_to_docx(doc_path: str, out_dir: str) -> Optional[str]:
    """用 LibreOffice 把 .doc 转成 .docx，返回新文件路径；失败返回 None。

    旧版 .doc 是二进制格式，python-docx / lxml 都无法直接处理，
    必须外部转换。LibreOffice 不在位时返回 None（调用方回退）。
    """
    soffice = find_soffice()
    if not soffice:
        logger.warning("未找到 LibreOffice，无法转换 .doc 原始文书（修改版将回退为纯文本重建）")
        return None

    try:
        # 用独立 user profile 目录，避免与用户正在运行的 LibreOffice 实例冲突
        profile = tempfile.mkdtemp(prefix="lo_profile_")
        r = subprocess.run(
            [soffice, "--headless", "--norestore",
             f"-env:UserInstallation=file:///{profile.replace(os.sep, '/')}",
             "--convert-to", "docx", "--outdir", out_dir, doc_path],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode != 0:
            logger.warning(f".doc 转换失败 (exit={r.returncode}): {(r.stderr or '')[:200]}")
            return None
        stem = os.path.splitext(os.path.basename(doc_path))[0]
        out = os.path.join(out_dir, f"{stem}.docx")
        if os.path.exists(out):
            logger.info(f".doc 已转换为 .docx: {os.path.basename(doc_path)}")
            return out
        logger.warning(f".doc 转换未产出文件: {doc_path}")
        return None
    except subprocess.TimeoutExpired:
        logger.warning(f".doc 转换超时: {doc_path}")
        return None
    except Exception as e:
        logger.warning(f".doc 转换异常: [{type(e).__name__}] {e}")
        return None
    finally:
        shutil.rmtree(profile, ignore_errors=True) if 'profile' in dir() else None


# ── 段落文本提取 ──

def _paragraph_text(p) -> str:
    """提取段落可见文本（含 <w:ins> 内的，供后续迭代处理已带修订的文档）"""
    parts = []
    for node in p.iter():
        tag = node.tag
        if tag == _qn("w:t") or tag == _qn("w:delText"):
            parts.append(node.text or "")
        elif tag == _qn("w:tab"):
            parts.append("\t")
        elif tag == _qn("w:br"):
            parts.append("\n")
    return "".join(parts)


def _normalize(text: str) -> str:
    """归一化用于比对：去掉所有空白与全角空格，便于容忍排版差异"""
    return re.sub(r"[\s\u3000]+", "", text or "")


def _iter_body_paragraphs(body):
    """按文档顺序产出 body 下所有段落（含表格单元格内的）。

    返回 [(paragraph_element, container_element, index_in_container)]。
    只取顶层与表格内的一层，避免钻进文本框等复杂结构。
    """
    out = []

    def walk(container):
        for child in list(container):
            if child.tag == _qn("w:p"):
                out.append((child, container, list(container).index(child)))
            elif child.tag == _qn("w:tbl"):
                for tr in child.findall(_qn("w:tr")):
                    for tc in tr.findall(_qn("w:tc")):
                        walk(tc)

    walk(body)
    return out


# ── 修订标记构造 ──

class _RevIdGen:
    """修订 id 生成器（w:id 需全文唯一）"""

    def __init__(self, start: int = 9000):
        self._n = start

    def next(self) -> str:
        self._n += 1
        return str(self._n)


def _make_ins(author: str, date: str, rev_id: str):
    el = _make_rev_element("w:ins", author, date, rev_id)
    return el


def _make_del(author: str, date: str, rev_id: str):
    return _make_rev_element("w:del", author, date, rev_id)


def _make_rev_element(tag: str, author: str, date: str, rev_id: str):
    from lxml import etree
    el = etree.Element(_qn(tag))
    el.set(_qn("w:id"), rev_id)
    el.set(_qn("w:author"), author)
    el.set(_qn("w:date"), date)
    return el


def _clone_run_shell(run):
    """克隆 run 的格式外壳（rPr），丢弃原有文本内容。

    这样新内容会继承原文书的字体/字号/加粗等格式。
    """
    from lxml import etree
    new_run = etree.Element(_qn("w:r"))
    rpr = run.find(_qn("w:rPr"))
    if rpr is not None:
        new_run.append(copy.deepcopy(rpr))
    return new_run


def _set_run_text(run, text: str, as_del_text: bool = False, tabs: int = 0, brs: int = 0):
    """设置 run 的文本（整段设置，不做字符切分）。

    tabs / brs 为该 run 中 <w:tab/> 与 <w:br/> 的数量：它们不是文本节点，
    删除整段时若不补回，段落标记删除后残留的制表位/换行会串到合并后的相邻段落。

    ⚠️ 文本中的 ``\\n`` 一律渲染为 <w:br/>（段内软换行）。**新增内容不得走
    这条路径** —— 新增段落必须用真实 <w:p> 分段（见 `_make_paragraphs_like`），
    否则软换行不产生新段落，段落级 pPr（居中/缩进/间距/编号）会作用于整块，
    版式与原文不一致，且修订标记会连成一大片无法逐项辨认。
    本函数的换行分支只服务于「删除原文中本来就存在的软换行」。
    """
    from lxml import etree
    # 清掉可能已存在的文本节点
    for t in run.findall(_qn("w:t")):
        run.remove(t)
    for t in run.findall(_qn("w:delText")):
        run.remove(t)

    tag = "w:delText" if as_del_text else "w:t"
    t = etree.SubElement(run, _qn(tag))
    # 保留首尾空格
    if text != text.strip():
        t.set(f"{{{XML_NS}}}space", "preserve")
    # 文本中的换行用 <w:br/> 表示
    if "\n" in text:
        run.remove(t)
        chunks = text.split("\n")
        for i, chunk in enumerate(chunks):
            if i:
                etree.SubElement(run, _qn("w:br"))
            if chunk:
                tt = etree.SubElement(run, _qn(tag))
                if chunk != chunk.strip():
                    tt.set(f"{{{XML_NS}}}space", "preserve")
                tt.text = chunk
    else:
        t.text = text

    for _ in range(max(0, int(tabs or 0))):
        etree.SubElement(run, _qn("w:tab"))
    for _ in range(max(0, int(brs or 0))):
        etree.SubElement(run, _qn("w:br"))


def _iter_paragraph_runs(p):
    """按文档顺序取出段落内所有 run，包含嵌套在 w:hyperlink / w:smartTag /
    w:sdt 等容器里的 run。

    只取 <w:p> 的直接子节点会漏掉超链接内的文本 —— 那部分内容不会被包进
    <w:del>，表现为「模型要求删除该段，但链接文字仍留在正文且无修订标记」。
    """
    return [el for el in p.iter() if el.tag == _qn("w:r")]


def _run_visible_text(r) -> str:
    """run 的可见文本（w:t 与 w:delText）"""
    parts = []
    for t in r:
        if t.tag in (_qn("w:t"), _qn("w:delText")):
            parts.append(t.text or "")
    return "".join(parts)


def _run_tab_count(r) -> int:
    """run 内 <w:tab/> 的数量（制表位不是文本节点，需单独搬移）"""
    return sum(1 for c in r if c.tag == _qn("w:tab"))


def _run_br_count(r) -> int:
    """run 内 <w:br/> 的数量（段内软换行同样不是文本节点）"""
    return sum(1 for c in r if c.tag == _qn("w:br"))


def _first_run(p):
    """取段落的第一个 run（作为格式模板）。

    优先直接子节点；整段都在超链接/内容控件里时回退到嵌套 run，
    否则新增段落拿不到字体模板（会退化成默认格式）。
    """
    for child in p:
        if child.tag == _qn("w:r"):
            return child
    nested = _iter_paragraph_runs(p)
    return nested[0] if nested else None


def _strip_paragraph_content(p):
    """清空段落的所有内容子节点，保留 pPr（段落格式）"""
    for child in list(p):
        if child.tag != _qn("w:pPr"):
            p.remove(child)


# ── 主流程 ──

def build_redlined_docx(original_path: str, revised_text: str,
                        author: str = REDLINE_AUTHOR) -> Optional[str]:
    """在原文书基础上生成带原生修订的 .docx，返回临时文件路径；失败返回 None。

    revised_text: LLM 输出的修改后正文（纯文本，不含标记；标记由本函数解析为修订）。
    """
    try:
        from lxml import etree
    except ImportError:
        logger.warning("lxml 未安装，无法生成原生修订文档")
        return None

    docx_path = original_path
    tmp_dir = None

    # .doc / .docm 等需先转换
    ext = os.path.splitext(original_path)[1].lower()
    if ext == ".doc":
        tmp_dir = tempfile.mkdtemp(prefix="docconv_")
        converted = convert_doc_to_docx(original_path, tmp_dir)
        if not converted:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return None
        docx_path = converted
    elif ext != ".docx":
        logger.info(f"原始文书非 docx（{ext}），无法保留原生格式")
        return None

    if not os.path.exists(docx_path):
        logger.warning(f"原始文书不存在: {docx_path}")
        return None

    work = tempfile.mkdtemp(prefix="redline_")
    try:
        # ── 解包 ──
        with zipfile.ZipFile(docx_path) as z:
            z.extractall(work)

        # 防御：外部 docx 可能含符号链接条目
        for dirpath, dirnames, filenames in os.walk(work):
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                if os.path.islink(fp):
                    os.remove(fp)

        doc_xml = os.path.join(work, "word", "document.xml")
        if not os.path.exists(doc_xml):
            logger.warning(f"docx 缺少 word/document.xml: {docx_path}")
            return None

        parser = etree.XMLParser(remove_blank_text=False)
        tree = etree.parse(doc_xml, parser)
        root = tree.getroot()
        body = root.find(_qn("w:body"))
        if body is None:
            logger.warning("docx 缺少 w:body")
            return None

        # ── 解析 LLM 输出为「段落级操作」 ──
        segments = _parse_marked_text(revised_text)
        rev_ids = _RevIdGen()
        from datetime import datetime, timezone
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        applied = _apply_segments_to_body(
            body, segments, author, date_str, rev_ids
        ) if segments else 0

        logger.info(f"原生修订：应用 {applied} 处改动")

        # ── 兜底自检：修订比例异常时放弃，避免产出「整篇被划掉」的文书 ──
        # 段落对齐一旦失败，会把大量原文判成删除 + 新增，生成的文书在 Word 里
        # 几乎全文带删除线，既无法审阅也比不发更糟。此处检测删除字符占比，
        # 超阈值即判定生成失败（调用方回退为普通副本 / 纯文本重建）。
        deleted_chars, ratio = _deleted_ratio(body)
        if deleted_chars >= _MIN_DELETED_CHARS and ratio > _MAX_DELETED_RATIO:
            logger.error(
                f"修订比例异常（删除 {deleted_chars} 字，占比 {ratio:.0%} > "
                f"{_MAX_DELETED_RATIO:.0%}），判定段落对齐失败，放弃生成修订版"
            )
            return None

        tree.write(doc_xml, xml_declaration=True, encoding="UTF-8", standalone=True)

        # ── 开启修订模式 ──
        _enable_track_changes(work)

        # ── 打包 ──
        out = tempfile.NamedTemporaryFile(
            suffix=".docx", prefix="修改版文书_", delete=False
        )
        out.close()
        _zip_dir(work, out.name)
        logger.info(f"修改版文书（原生修订）已生成: {out.name}")
        return out.name

    except Exception as e:
        logger.error(f"生成原生修订文档失败: [{type(e).__name__}] {e}", exc_info=True)
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# 删除字符占比超过该阈值即判定段落对齐失败，放弃生成修订版。
# 正常文书即便大幅改写，删除占比也远低于此；只有对齐彻底失败
# （整篇被判成删除 + 新增）时才会逼近 1.0。
#
# 同时要求**绝对删除量**超过 `_MIN_DELETED_CHARS` 才触发：
# 短文书（几百字的测试样例、便签式协议）里「改掉大半内容」是正常需求，
# 仅凭比例会把合法的大幅修订误判为对齐失败。
_MAX_DELETED_RATIO = 0.60
_MIN_DELETED_CHARS = 800


def _deleted_ratio(body) -> tuple:
    """统计 <w:del> 覆盖的字符数，返回 (删除字符数, 删除占比)。

    占比的分母为「原文可见字符总量」= 仍在正文中的字符 + 被删除的字符。
    返回 (0, 0.0) 表示全文无文字，调用方应跳过自检。
    """
    deleted = 0
    kept = 0
    for node in body.iter():
        if node.tag == _qn("w:delText"):
            deleted += len(node.text or "")
        elif node.tag == _qn("w:t"):
            kept += len(node.text or "")
    total = deleted + kept
    if total <= 0:
        return (0, 0.0)
    return (deleted, deleted / total)


def _enable_track_changes(work_dir: str):
    """在 word/settings.xml 写入 <w:trackChanges/>"""
    from lxml import etree
    settings_xml = os.path.join(work_dir, "word", "settings.xml")
    if not os.path.exists(settings_xml):
        logger.warning("docx 无 settings.xml，无法开启修订模式")
        return
    try:
        parser = etree.XMLParser(remove_blank_text=False)
        tree = etree.parse(settings_xml, parser)
        root = tree.getroot()
        if root.find(_qn("w:trackChanges")) is None:
            root.insert(0, etree.Element(_qn("w:trackChanges")))
            tree.write(settings_xml, xml_declaration=True, encoding="UTF-8", standalone=True)
    except Exception as e:
        logger.warning(f"写入 trackChanges 失败: [{type(e).__name__}] {e}")


def _zip_dir(src_dir: str, out_path: str):
    """从目录内部打包成 docx（先删目标，避免残留条目）"""
    if os.path.exists(out_path):
        os.remove(out_path)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(src_dir):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                arc = os.path.relpath(full, src_dir).replace(os.sep, "/")
                zf.write(full, arc)


def _parse_marked_text(text: str) -> list[tuple[str, str]]:
    """把 LLM 输出的标记文本解析为 [(kind, content)] 序列。

    kind: "normal" | "add" | "modify" | "delete"
    modify 视为「删除原文 + 新增修改后文本」，符合 Word 修订语义。
    """
    marker_re = re.compile(r"【(/?)(新增|修改|删除)】")
    tag_map = {"新增": "add", "修改": "modify", "删除": "delete"}

    segments = []
    stack = []
    pos = 0
    for m in marker_re.finditer(text):
        is_close = m.group(1) == "/"
        tag = tag_map[m.group(2)]
        if m.start() > pos:
            cur = stack[-1] if stack else "normal"
            segments.append((cur, text[pos:m.start()]))
        if is_close:
            if stack and stack[-1] == tag:
                stack.pop()
        else:
            stack.append(tag)
        pos = m.end()
    if pos < len(text):
        cur = stack[-1] if stack else "normal"
        segments.append((cur, text[pos:]))

    # 合并相邻同类
    merged = []
    for kind, seg in segments:
        if not seg:
            continue
        if merged and merged[-1][0] == kind:
            merged[-1] = (kind, merged[-1][1] + seg)
        else:
            merged.append((kind, seg))
    return merged


def _apply_segments_to_body(body, segments, author, date_str, rev_ids) -> int:
    """把解析出的段落级操作应用到文档 body。

    对齐以「段落」为单位：原文档中段落内可能含软换行（<w:br/>），
    例如「甲方：…\\n乙方：…」是**同一个** w:p。因此这里不能按 \\n 拆分，
    否则会把一个已有段落误判成两段新内容，产生错误的「整段新增」修订。

    策略：用「修订后全文」与「原文」做段落级对齐，逐段判定：
    - 匹配 → 不动
    - 新增 → 在锚点后插入带 <w:ins> 的段落
    - 删除 → 原段落所有 run 包 <w:del>，并标记段落标记删除
    - 修改 → 原 run 包 <w:del> + 其后插入 <w:ins> 新段落
    """
    entries_all = _iter_body_paragraphs(body)
    if not entries_all:
        return 0

    # 剔除空段落（仅含空白/软换行）后再对齐。
    # 真实文书里大量存在用于占位的空段落，而 LLM 输出的修订全文不会保留它们；
    # 若不剔除，这些空段在 LCS 里找不到对应项，会把后续所有段落挤成
    # 一个巨大的 replace 区间，导致整篇被标成「删除 + 新增」。
    entries = []
    orig_texts = []
    for p, container, idx in entries_all:
        norm = _normalize(_paragraph_text(p))
        if not norm:
            continue
        entries.append((p, container, idx))
        orig_texts.append(norm)

    if not entries:
        return 0

    # 构造目标段落序列：空行必定拆段；单换行则按行文形态判断
    # （见 `_split_target_paragraphs` / `_looks_like_standalone_paragraph`）
    target = []
    for kind, content in segments:
        for raw in _split_target_paragraphs(content):
            text = raw.strip()
            if not text:
                continue
            target.append((kind if kind != "normal" else "normal", text))

    if not target:
        return 0

    return _diff_apply(entries, orig_texts, target, author, date_str, rev_ids)


# 行首形态：命中则视为「独立段落」，单个换行也要拆段。
# 背景：LLM 输出的标记块里，换行绝大多数是单个 \n，而不是空行。
# 若一律当成段内软换行，会把「天津市 / 小型建设工程施工合同 /（JF-2001-015）」
# 三行压成一个段落 —— 与原文的三个独立段落对不上，段落级 LCS 只能判成
# 整段删除 + 整段新增，表现为封面被整块划掉后又重复插入一份。
_PARA_HEAD_RE = re.compile(
    r"^\s*(?:"
    r"第[一二三四五六七八九十百零〇\d]+[条章节款项目]"      # 第X条 / 第X章
    r"|[一二三四五六七八九十]+[、．.]"                      # 一、 / 十、
    r"|[（(]\s*\d+\s*[）)]"                                # （1） / (2)
    r"|\d+\s*[、．.]\s*(?=\S)"                             # 1. / 2、
    r"|[^\n：:]{1,12}[：:]"                                # 「工程名称：」「甲方：」等短标签
    r")"
)

# 行尾为「逗号/顿号/冒号/开括号」等**非终结**标点时，该行几乎肯定是续行
# （句子还没说完就换行了），此时不拆段。
_CONT_RE = re.compile(r"[，,、：:（(「『【]\s*$")


def _looks_like_standalone_paragraph(line: str) -> bool:
    """判断一行是否「看起来是一个独立的段落」。

    仅用于决定**标记块内**单个换行该拆段还是该保留为软换行。

    判据：
    1. 行首是条款/编号/短标签（「第三条」「一、」「（1）」「工程名称：」）→ 独立段；
    2. 行尾是逗号/顿号/冒号等**非终结**标点 → 续行（句子没说完就换行了），不独立；
    3. 短行（≤20 字且不含句中标点）→ 独立段，覆盖封面「天津市」这类标题行；
    4. 其余 → 不独立（普通长句）。

    ⚠️ 判据里**不能**把「行尾是句号」当作独立段落的充分条件：
    长正文几乎每句都以句号收尾，那样会把每一句都拆成一段，
    把「一段一句话」的排版彻底打散。句末标点只在**短行**上才有指示意义，
    已由判据 3 覆盖。
    """
    s = (line or "").strip()
    if not s:
        return False
    # 续行优先：句子未结束就换行
    if _CONT_RE.search(s):
        return False
    # 条款/编号/短标签开头 → 明确的新段落
    if _PARA_HEAD_RE.match(s):
        return True
    # 短行且无句中标点 → 标题/字段行
    if len(s) <= 20 and not re.search(r"[，,。；;：:]", s):
        return True
    return False


def _split_target_paragraphs(content: str) -> list[str]:
    """把一段标记内容拆成目标段落。

    拆段规则（按优先级）：
    1. **空行**（连续两个及以上换行）一定拆段；
    2. 单个换行：两侧任一行「看起来是独立段落」（见
       `_looks_like_standalone_paragraph`）时拆段；
    3. 否则保留为段内软换行 —— 用于原文书常见的
       ``甲方：…\\n乙方：…`` 这类本来就写在同一段里的结构。

    第 2 条是修复「封面被整块划掉后重复插入」的关键：LLM 用单个换行
    表示段落分隔是常态，全部当成软换行会让目标段数与原文段数对不上，
    段落级对齐只能退化为整段删除 + 整段新增。
    """
    if content is None:
        return []
    # 统一换行符
    text = content.replace("\r\n", "\n").replace("\r", "\n")

    out = []
    # 先用空行切成「块」，再在块内按语义决定是否拆行
    for block in re.split(r"\n[ \t\u3000]*\n+", text):
        if not block.strip():
            continue
        lines = block.split("\n")
        # 单行块：原样保留
        if len(lines) == 1:
            out.append(block)
            continue

        # 逐行判断是否在「上一行末与当前行首之间」断开。
        # 判据以**上一行**为主：
        #   - 上一行以逗号/顿号等非终结标点收尾 → 句子没说完，是续行，不断开；
        #   - 否则看当前行的行首是否像新段落的开头（条款号/短标签/句末标点）。
        cur = [lines[0]]
        for prev, ln in zip(lines, lines[1:]):
            if _CONT_RE.search(prev.strip()):
                # 上一行结尾是「未完结」标点 → 必定是同一段的续行
                cur.append(ln)
                continue
            if _looks_like_standalone_paragraph(prev):
                out.append("\n".join(cur))
                cur = [ln]
                continue
            if _looks_like_standalone_paragraph(ln):
                out.append("\n".join(cur))
                cur = [ln]
                continue
            cur.append(ln)
        out.append("\n".join(cur))

    return out


# ── 段落内字符级对齐 ──

# 差异占比超过该阈值时，放弃细粒度对齐，回退为「整段删除 + 整段新增」。
# 目的：整句重写时逐词对齐会产出大量碎片化的 del/ins 片段，可读性反而更差；
# 此时整段替换更接近人工审阅的阅读习惯。
_INLINE_FALLBACK_RATIO = 0.6

# 「1 个原文段落 ↔ 最多几个目标段落」的细化上限。
# 用于处理「原文一段被拆成多段」的形态；设小值避免把大段无关内容糊成一条修订。
_MAX_MERGE_SPAN = 3


def _inline_diff_runs(old_text: str, new_text: str):
    """把段落内的「原文 → 新文」比对成字符级修订片段。

    返回 [(kind, text)]，kind ∈ {"equal", "del", "ins"}，按顺序拼回即为新文
    （equal + ins 部分）与原文（equal + del 部分）。

    仅在**差异占比 ≤ _INLINE_FALLBACK_RATIO** 时启用细粒度；
    差异过大（接近整句重写）时返回 None，由调用方回退为整段替换。

    注意：比对基于 `_normalize` 之外的**原始文本**——空白差异也算改动，
    否则会出现「标记的位置与实际文字对不上」的错位标注。
    """
    import difflib

    if old_text == new_text:
        return None
    if not old_text or not new_text:
        return None

    sm = difflib.SequenceMatcher(None, old_text, new_text, autojunk=False)
    ops = sm.get_opcodes()

    # 差异占比：改动字符数 / 较长一侧长度
    changed = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in ops if tag != "equal")
    ratio = changed / max(len(old_text), len(new_text))
    if ratio > _INLINE_FALLBACK_RATIO:
        return None

    pieces = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            pieces.append(("equal", new_text[j1:j2]))
        elif tag == "delete":
            pieces.append(("del", old_text[i1:i2]))
        elif tag == "insert":
            pieces.append(("ins", new_text[j1:j2]))
        else:  # replace
            pieces.append(("del", old_text[i1:i2]))
            pieces.append(("ins", new_text[j1:j2]))

    # 合并相邻同类片段，减少 Word 修订条数
    merged = []
    for kind, seg in pieces:
        if not seg:
            continue
        if merged and merged[-1][0] == kind:
            merged[-1] = (kind, merged[-1][1] + seg)
        else:
            merged.append((kind, seg))

    merged = _coalesce_short_fragments(merged)

    # 全是 equal（理论上不会到这里）或无任何改动 → 不做细粒度
    if not any(k != "equal" for k, _ in merged):
        return None
    return merged


def _coalesce_short_fragments(pieces: list) -> list:
    """合并相邻同类的修订片段，减少 Word 里的碎片标记。

    ⚠️ 设计约束（重要）：**绝不能改变片段的先后顺序，也不能跨 equal 片段
    搬运文本**。`del`/`ins` 片段交织的顺序就是原文/新文的字符顺序，一旦为了
    「看起来整齐」把删除文本与新增文本各自归拢，接受或拒绝修订后得到的
    文本就会错位（实测会把「于2026年12月31日前」还原成「于20261231年月日前」）。
    因此这里只做**安全的就地合并**：

    1. 相邻且同类的片段直接拼接（`ins"A" ins"B"` → `ins"AB"`）；
    2. 只隔一个**空** equal 片段的同类片段也合并（空片段不承载任何文字）。

    被短 equal 隔开的 del/ins 交替序列（如「空格填数字」）保持原样：
    语序正确性优先于观感，碎片多但每处修订都忠实对应原文与新文。
    """
    if len(pieces) < 3:
        # 仍然做一次相邻同类合并
        pass

    out = []
    for kind, text in pieces:
        if not text:
            continue
        out.append((kind, text))

    # 反复扫描，直到不再变化（处理 "A" eq"" "A" 这类情况）
    changed = True
    while changed:
        changed = False
        merged = []
        for kind, text in out:
            if merged and merged[-1][0] == kind:
                merged[-1] = (kind, merged[-1][1] + text)
                changed = True
            else:
                merged.append((kind, text))
        # 吸收空 equal
        out = [(k, t) for k, t in merged if not (k == "equal" and not t)]
        if out != merged:
            changed = True
    return out


def _split_paragraph_with_pieces(p, pieces, author, date_str, rev_ids) -> bool:
    """把含换行的 ins 片段拆成多个真实段落（而非段内软换行）。

    `_replace_paragraph_inline` 在 ins 片段里发现 ``\\n`` 时转交本函数。
    场景：原段落只有一行，修订后在中间另起一段（如「…第 2 种方式解决：」
    后面新增两行列举）。若不处理，这两行会以 <w:br/> 挤在同一段里。

    做法：按 ins 片段中的 ``\\n`` 把整个片段序列切成若干「行组」；
    第一行组留在原段落，其余行组各建一个新段落。删除片段（del）只在
    它所属的那一行组里生效，保证接受修订后文本正确。

    要求原段落是纯 run 结构（由调用方保证）。
    """
    from lxml import etree

    if not pieces:
        return False

    runs = [r for r in p if r.tag == _qn("w:r")]
    src_run = runs[0] if runs else _first_run(p)
    ref_rpr = src_run.find(_qn("w:rPr")) if src_run is not None else None
    src_ppr = p.find(_qn("w:pPr"))

    # 按 ins 内部的换行，把片段序列切成「行组」。
    # del/equal 片段本身不含换行（换行只可能来自目标文本），归入当前行组。
    groups = [[]]                       # [ [(kind,text), ...] ]
    for kind, text in pieces:
        if not text:
            continue
        if kind == "ins" and "\n" in text:
            parts = text.split("\n")
            for i, seg in enumerate(parts):
                if i:
                    groups.append([])
                if seg:
                    groups[-1].append(("ins", seg))
        else:
            groups[-1].append((kind, text))

    # 去掉尾部空组
    while groups and not any(t for _, t in groups[-1]):
        groups.pop()
    if len(groups) <= 1:
        return False

    def _build_nodes(grp):
        """把一组片段渲染成 w:p 的直接子节点"""
        nodes = []
        for kind, text in grp:
            if not text:
                continue
            if kind == "equal":
                r = etree.Element(_qn("w:r"))
                if ref_rpr is not None:
                    r.append(copy.deepcopy(ref_rpr))
                _set_run_text(r, text)
                nodes.append(r)
            elif kind == "del":
                d = _make_del(author, date_str, rev_ids.next())
                r = etree.Element(_qn("w:r"))
                if ref_rpr is not None:
                    r.append(copy.deepcopy(ref_rpr))
                _set_run_text(r, text, as_del_text=True)
                d.append(r)
                nodes.append(d)
            else:
                i_el = _make_ins(author, date_str, rev_ids.next())
                r = etree.Element(_qn("w:r"))
                if ref_rpr is not None:
                    r.append(copy.deepcopy(ref_rpr))
                _set_run_text(r, text)
                i_el.append(r)
                nodes.append(i_el)
        return nodes

    # 第一组：就地替换原段落内容
    _strip_paragraph_content(p)
    for node in _build_nodes(groups[0]):
        p.append(node)

    # 其余组：各自建新段落，插在原段落之后
    anchor = p
    for grp in groups[1:]:
        new_p = etree.Element(_qn("w:p"))
        if src_ppr is not None:
            new_p.append(copy.deepcopy(src_ppr))
        for node in _build_nodes(grp):
            new_p.append(node)
        anchor.addnext(new_p)
        anchor = new_p
    return True


def _replace_paragraph_inline(p, pieces, author, date_str, rev_ids) -> bool:
    """按字符级片段重写段落内容，产出细粒度的原生修订标记。

    铁律遵守（见模块头）：
    - `<w:ins>` / `<w:del>` 必须是 `<w:p>` 的**直接子节点**（`<w:r>` 的兄弟）；
    - 每个标记包一个完整的 `<w:r>`，绝不在 `<w:t>` 内部做字符切分；
    - 格式统一取自原段落首个 run 的 rPr，保证与原文书字体一致。

    段落的 pPr 与段落标记保持不变（段落仍然存在，只是内容被逐词修订）。
    """
    from lxml import etree

    runs = [r for r in p if r.tag == _qn("w:r")]
    src_run = runs[0] if runs else _first_run(p)
    ref_rpr = src_run.find(_qn("w:rPr")) if src_run is not None else None

    def _new_run(text: str, as_del: bool):
        r = etree.Element(_qn("w:r"))
        if ref_rpr is not None:
            r.append(copy.deepcopy(ref_rpr))
        _set_run_text(r, text, as_del_text=as_del)
        return r

    # ⚠️ 新增内容里绝不能出现 `\n` → `_set_run_text` 会把它渲染成 <w:br/>
    # 软换行，段落级格式（居中/缩进/行距）就会作用于整块，版式与原文不一致，
    # 修订标记也连成一大片无法逐项辨认。
    # 出现在 ins 片段里的换行，说明该段落被拆成了多段 → 转交 `_split_paragraph_with_pieces`
    # 处理（真正的 <w:p> 分段）。
    if any("\n" in t for k, t in pieces if k == "ins"):
        return _split_paragraph_with_pieces(p, pieces, author, date_str, rev_ids)

    # 先构造全部替换节点，再统一替换，避免边遍历边改动
    new_nodes = []
    for kind, text in pieces:
        if kind == "equal":
            new_nodes.append(_new_run(text, as_del=False))
        elif kind == "del":
            d = _make_del(author, date_str, rev_ids.next())
            d.append(_new_run(text, as_del=True))
            new_nodes.append(d)
        else:  # ins
            i = _make_ins(author, date_str, rev_ids.next())
            i.append(_new_run(text, as_del=False))
            new_nodes.append(i)

    if not new_nodes:
        return False

    # 找到第一个非 pPr 子节点的位置，把旧内容整段替换掉
    anchor = None
    for child in list(p):
        if child.tag != _qn("w:pPr"):
            anchor = child
            break

    if anchor is None:
        # 段落原本无内容（只有 pPr）→ 直接追加
        for node in new_nodes:
            p.append(node)
        return True

    anchor.addprevious(new_nodes[0])
    prev = new_nodes[0]
    for node in new_nodes[1:]:
        prev.addnext(node)
        prev = node

    # 删除旧的内容节点（anchor 及其后所有非 pPr 子节点）
    for child in list(p):
        if child.tag == _qn("w:pPr"):
            continue
        if child in new_nodes:
            continue
        p.remove(child)

    return True


def _try_inline_replace(entries, target, i1, i2, j1, j2,
                        author, date_str, rev_ids) -> list:
    """尝试把 [i1,i2) 的原文段落与 [j1,j2) 的目标段落做**段落内**细粒度修订。

    返回**已细化的 (原文下标, 目标下标) 列表**（可能为空）。
    调用方据此精确跳过这些段落，把剩余部分交给整段替换。

    一个 `replace` 区间里往往混着两类内容：真正被改动的段落，以及纯新增/纯删除
    的段落（例如末尾追加一条新条款，会把前面几段也一起卷进同一个 replace）。
    因此这里只对区间**首尾能一一对应的部分**做细化：

    - 从区间头部开始，逐段配对，直到某一段不再满足细化条件；
    - 从区间尾部继续逐段配对（处理「前面插了段落、后面才是改动」的情形）；
    - 每次配对失败即停止，不做跳跃式配对，保证被细化的段落总是连续的。

    单段满足以下全部条件才细化：
    1. 原文段落必须是「纯 run 结构」——含书签/域/超链接等嵌套容器时，
       重写段落内容会连带删掉这些结构，回退整段替换更稳妥；
    2. 段落文本非空；
    3. 字符级差异算得出来且未超 `_INLINE_FALLBACK_RATIO`。

    比对以**段落全文**为单位：原段落文本 = 该段所有 run 的可见文本拼接；
    段落内的 `<w:br/>`（软换行）会被 `_paragraph_text` 还原为 `\\n`，
    因此带软换行的段落也能正确对齐。
    """
    n_old = i2 - i1
    n_new = j2 - j1
    if n_old <= 0 or n_new <= 0:
        return False
    # 显式整段删除不参与细化（那是明确的整段删除意图）
    if any(target[j][0] == "delete" for j in range(j1, j2)):
        return False

    def _pair_is_inlinable(oi: int, tj: int):
        """返回 (原文段, 片段列表) 或 None（1 对 1）"""
        p = entries[oi][0]
        old_text = _paragraph_text(p)
        new_text = target[tj][1]
        if not old_text.strip() or not new_text.strip():
            return None
        if any(child.tag != _qn("w:pPr") and child.tag != _qn("w:r") for child in p):
            return None
        pieces = _inline_diff_runs(old_text, new_text)
        if pieces is None:
            return None
        return (p, pieces)

    def _pair_is_inlinable_1n(oi: int, tj: int, max_span: int):
        """1 对多：把一个原文段落与**连续多个**目标段落比对。

        处理「一段被拆成多段」的形态：原文里靠软换行写在一段的若干条目，
        改写后被拆成多个独立段落。1 对 1 配对必然失败，只能回退整段替换，
        Word 里就显示为「整段删掉 + 整段重加」。

        做法：把连续 k 个目标段落的文本用 ``\\n`` 拼接，与原段落做字符级比对。
        返回 (原文段, 片段, 消耗的目标段数) 或 None。

        ⚠️ 只接受 ``k >= 2`` 的匹配，**不返回 k=1**：
        k=1 是普通的一对一情形，由 `_pair_is_inlinable` 负责；若这里也接受
        k=1，朴素实现会在 k=1 就命中（例如把后几行当作「被删除」），
        真正的「一段拆多段」永远没机会被识别。因此这里显式从 k=2 起找。

        k >= 2 且原段落文本恰好等于拼接结果时，属于**纯结构拆分**（文字没变，
        只是段落数变了），返回全 equal 片段，交由 `_split_paragraph_into` 处理。
        """
        p = entries[oi][0]
        old_text = _paragraph_text(p)
        if not old_text.strip():
            return None
        if any(child.tag != _qn("w:pPr") and child.tag != _qn("w:r") for child in p):
            return None
        if target[tj][0] == "delete":
            return None
        for k in range(2, max_span + 1):
            if tj + k > j2:
                break
            # 目标侧含显式【删除】标记时不参与（那是明确的删除意图）
            if any(target[tj + m][0] == "delete" for m in range(k)):
                break
            merged = "\n".join(target[tj + m][1] for m in range(k))
            if not merged.strip():
                continue
            pieces = _inline_diff_runs(old_text, merged)
            if pieces is not None:
                return (p, pieces, k)
            # 文本完全相同、但目标段数 > 1 → 纯结构拆分
            if old_text == merged:
                return (p, [("equal", old_text)], k)
        return None

    # ── 头部逐段配对 ──
    # 先试 1 对 1；失败再试 1 对多（一段拆成多段）。
    # `_MAX_MERGE_SPAN` 限制一次最多吞并几段，避免把大段无关内容糊成一条修订。
    head = []          # [(原文下标, [目标下标...], 原文段, 片段)]
    n = min(n_old, n_new)
    k = 0
    while k < n:
        oi, tj = i1 + k, j1 + k
        # 先试 1 对多：当一个原文段落的文本恰好等于**连续多个**目标段落的
        # 拼接时，说明是「原文一段被拆成多段」，优先按拆分处理。
        # 若先试 1 对 1，会把后面几行判成「被删除」（文本少了，但差异占比
        # 未必超阈值），于是拆分被误当成删减，后面几段又被当成纯新增。
        got1n = _pair_is_inlinable_1n(oi, tj, _MAX_MERGE_SPAN)
        if got1n is not None:
            p, pieces, consumed = got1n
            head.append((oi, list(range(tj, tj + consumed)), p, pieces))
            k += consumed
            continue
        got = _pair_is_inlinable(oi, tj)
        if got is not None:
            head.append((oi, [tj], got[0], got[1]))
            k += 1
            continue
        break

    # ── 尾部逐段配对（不与头部重叠）──
    tail = []
    while k + len(tail) < n:
        off = len(tail)
        oi, tj = i2 - 1 - off, j2 - 1 - off
        got = _pair_is_inlinable(oi, tj)
        if got is None:
            break
        tail.append((oi, [tj], got[0], got[1]))
    tail.reverse()
    plans = head + tail

    handled = []
    for oi, tjs, p, pieces in plans:
        # 「纯结构拆分」：文本未变（片段全是 equal），只是原文一段要拆成多段。
        # 此时不能走 `_replace_paragraph_inline`（它会把整段替换成同样的文本，
        # 产生一堆无意义的修订），而应把原段落按目标行拆开。
        if len(tjs) > 1 and all(k == "equal" for k, _ in pieces):
            if _split_paragraph_into(p, target, tjs, author, date_str, rev_ids):
                for tj in tjs:
                    handled.append((oi, tj))
            continue
        if not _replace_paragraph_inline(p, pieces, author, date_str, rev_ids):
            continue
        if len(tjs) == 1:
            handled.append((oi, tjs[0]))
            continue
        # 1 对多：承载细化的是原文段落本身；多出来的目标段落需要**追加新增段落**，
        # 否则接受修订后这些内容会丢失。
        newps = _make_paragraphs_from_template(
            p, target, tjs[1:], author, date_str, rev_ids
        )
        anchor = p
        for np in newps:
            anchor.addnext(np)
            anchor = np
        # 覆盖到的下标全部登记，调用方据此从剩余区间里剔除
        for tj in tjs:
            handled.append((oi, tj))
    # 返回**已细化的下标对**，调用方据此精确跳过这些段落，
    # 只把剩余部分交给整段替换（头部与尾部都可能命中，不能用「前 n 段」近似）。
    return handled


def _diff_apply(entries, orig_texts, target, author, date_str, rev_ids) -> int:
    """基于 LCS 的差异应用：段落级对齐 + 段落内字符级细化。

    段落级：用 difflib 对齐原文段落与目标段落，判定 equal/replace/insert/delete。
    段落内：replace 区间内若**原文段数与目标段数一一对应**，则对每对段落做
    字符级对齐，只标注真正改动的词句（`_replace_paragraph_inline`）；
    对不上（整段新增/删除/行数变化）或差异过大时，回退为原有的
    「整段删除 + 整段新增」，保证语义与既有行为一致。
    """
    import difflib

    target_texts = [_normalize(t[1]) for t in target]
    sm = difflib.SequenceMatcher(None, orig_texts, target_texts, autojunk=False)

    # 显式标记的类型必须生效，不能被 LCS 的 equal 吞掉：
    # LLM 用【删除】标出的段落，其文本与原文完全相同，纯文本 diff 会判为
    # 「未改动」，导致该删除被静默忽略。这里把带标记的段落从 equal 区间中
    # 摘出来，强制作为改动处理。
    forced = _extract_forced_ops(orig_texts, target, sm)

    if forced is None:
        return 0
    ops = forced

    changes = 0
    # 从后往前处理，避免插入导致后续索引错位
    for op in reversed(ops):
        tag, i1, i2, j1, j2, kinds = op
        if tag == "equal":
            continue
        if tag in ("replace", "delete_marked"):
            # ── 优先尝试段落内字符级细化 ──
            # 只标注真正改动的词句，段落本身保持不动。
            # 区间首尾能一一对应的段落会就地细化；返回细化成功的段数 n_inlined，
            # 这些段落已处理完毕，剩余部分（纯新增/纯删除/差异过大）继续走
            # 下面的「整段删除 + 整段新增」回退路径。
            n_inlined = _try_inline_replace(entries, target, i1, i2, j1, j2,
                                            author, date_str, rev_ids)
            if n_inlined:
                changes += len(n_inlined)
                # 已细化的段落就地改好了，从剩余区间里剔除；
                # 头部与尾部都可能命中，逐个排除比「收缩前缀」更精确。
                done_old = {oi for oi, _ in n_inlined}
                done_new = {tj for _, tj in n_inlined}
                rest_old = [k for k in range(i1, i2) if k not in done_old]
                rest_new = [k for k in range(j1, j2) if k not in done_new]
                if not rest_old and not rest_new:
                    continue
                # 剩余部分必须仍是连续区间才走整段替换；否则放弃（保守）。
                if (rest_old and rest_old != list(range(rest_old[0], rest_old[-1] + 1))) or \
                   (rest_new and rest_new != list(range(rest_new[0], rest_new[-1] + 1))):
                    logger.warning("细化后剩余段落不连续，跳过回退处理（保守）")
                    continue
                if rest_old:
                    i1, i2 = rest_old[0], rest_old[-1] + 1
                else:
                    i1 = i2
                if rest_new:
                    j1, j2 = rest_new[0], rest_new[-1] + 1
                else:
                    j1 = j2
                if i1 >= i2 and j1 >= j2:
                    continue

            # ── 回退：整段删除 + 整段新增 ──
            # 注意：格式模板必须取自**被替换的原文段落**，而不是其前一段，
            # 否则新增内容会继承错误的字体/缩进（例如把正文格式套到标题上）。
            kind = _kind_of(target, j1, j2)
            anchor_p = entries[i2 - 1][0] if i2 - 1 >= 0 else None
            # ⚠️ 必须在删除之前取出格式模板：_delete_paragraph 会把原 run
            # 移进 <w:del>，之后再取就找不到 run，导致新增内容丢失字体。
            style_run = None
            if i1 < len(entries):
                style_run = _first_run(entries[i1][0])
            if style_run is None and anchor_p is not None:
                style_run = _first_run(anchor_p)
            style_rpr = copy.deepcopy(style_run.find(_qn("w:rPr"))) \
                if style_run is not None and style_run.find(_qn("w:rPr")) is not None \
                else None
            style_ppr = copy.deepcopy(entries[i1][0].find(_qn("w:pPr"))) \
                if i1 < len(entries) and entries[i1][0].find(_qn("w:pPr")) is not None \
                else None
            style_p = entries[i1][0] if i1 < len(entries) else anchor_p
            for k in range(i1, i2):
                if _delete_paragraph(entries[k][0], author, date_str, rev_ids):
                    changes += 1
            insert_after = anchor_p
            for j in range(j1, j2):
                # 目标中显式标为 delete 的段落，不产生新增内容
                if target[j][0] == "delete":
                    continue
                newps = _make_paragraph_with_style(
                    target[j][1], style_p, style_ppr, style_rpr,
                    author, date_str, rev_ids
                )
                if not newps or insert_after is None:
                    continue
                for newp in newps:
                    insert_after.addnext(newp)
                    insert_after = newp
                    changes += 1
        elif tag == "delete":
            for k in range(i1, i2):
                if _delete_paragraph(entries[k][0], author, date_str, rev_ids):
                    changes += 1
        elif tag == "insert":
            # 插入锚点：i1 == 0 表示插在文档最前面，此时没有「前一段」可作锚点，
            # 必须让 anchor_p 为 None，走下面的 addprevious 分支插到第一段之前。
            # 若沿用旧的钳位写法（退化为 entries[0]），插入点变成「第一段之后」，
            # 会把新增的前置条款放到错误位置。
            anchor_p = entries[i1 - 1][0] if i1 >= 1 else None
            kind = _kind_of(target, j1, j2)
            insert_after = anchor_p
            # 文首插入没有「前一段」，但仍需要一个格式模板 → 借用原第一段。
            style_anchor = anchor_p if anchor_p is not None else (entries[0][0] if entries else None)
            for j in range(j1, j2):
                newps = _make_paragraph_like(
                    style_anchor, target[j][1], kind, author, date_str, rev_ids
                )
                if not newps:
                    continue
                for newp in newps:
                    if insert_after is not None:
                        insert_after.addnext(newp)
                        insert_after = newp
                        changes += 1
                    elif entries:
                        # 插入点在文档最前面：此时没有「前一段」可用作锚点。
                        # 原先在这种情况直接跳过（insert_after 为 None），会导致
                        # 「在正文开头新增一段」的修订被静默丢弃；退化为「插到第一段
                        # 之后」则会把前置条款挪到错误位置。正确做法是插到第一段之前。
                        entries[0][0].addprevious(newp)
                        insert_after = newp
                        changes += 1
    return changes


def _extract_forced_ops(orig_texts, target, sm):
    """把带显式标记的段落从 LCS 的 equal 区间中摘出，强制作为改动。

    LLM 用【删除】标记某段时，该段文本与原文完全相同，纯文本 diff 会判为
    「未改动」而静默忽略这条指令。因此在 diff 结果之上做一次重切：
    凡目标段落 kind 非 normal 的，其对应的 opcode 一律不保留为 equal。

    返回 [(tag, i1, i2, j1, j2, kinds)]，已按原始顺序排好；
    数据不足以安全重切时返回 None（调用方回退为纯 diff 行为）。
    """
    kinds = [k for k, _ in target]

    # 快速路径：没有显式标记，直接用 diff 结果
    if all(k == "normal" for k in kinds):
        return [(t, i1, i2, j1, j2, [])
                for t, i1, i2, j1, j2 in sm.get_opcodes()]

    # 校验：只有当 diff 认为两侧完全一致、且长度相同时，才能安全地按位置重切。
    # 一旦内容真的不同，diff 的 i/j 已经不再一一对应，此时不做重切，
    # 以免把错位的段落标成删除。
    ops = list(sm.get_opcodes())
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal" and (i2 - i1) != (j2 - j1):
            logger.warning("段落对齐长度不一致，跳过显式标记重切（保守处理）")
            return [(t, a, b, c, d, []) for t, a, b, c, d in ops]

    out = []
    for tag, i1, i2, j1, j2 in ops:
        if tag != "equal":
            out.append((tag, i1, i2, j1, j2, kinds[j1:j2]))
            continue
        # 把 equal 区间按「是否含显式标记」切成若干段
        k = 0
        n = i2 - i1
        while k < n:
            j = j1 + k
            marked = kinds[j] != "normal"
            k2 = k
            while k2 < n and (kinds[j1 + k2] != "normal") == marked:
                k2 += 1
            if marked:
                # 该区间内目标段落带标记 → 对应原文段落按标记强制处理
                seg_kinds = kinds[j1 + k:j1 + k2]
                if all(sk == "add" for sk in seg_kinds):
                    out.append(("insert", i1 + k, i1 + k, j1 + k, j1 + k2, seg_kinds))
                else:
                    out.append(("delete_marked", i1 + k, i1 + k2, j1 + k, j1 + k2, seg_kinds))
            else:
                out.append(("equal", i1 + k, i1 + k2, j1 + k, j1 + k2, []))
            k = k2
    return out


def _kind_of(target, j1, j2) -> str:
    """取该区间的主类型（add/modify/delete/normal）"""
    kinds = [target[j][0] for j in range(j1, j2)]
    for k in ("add", "modify", "delete"):
        if k in kinds:
            return k
    return "add"


def _delete_paragraph(p, author, date_str, rev_ids) -> bool:
    """把段落内所有 run 包进 <w:del>，并标记段落标记删除（等于整段删除）。

    <w:del> 是 <w:r> 的兄弟节点（<w:p> 的直接子节点）—— 铁律 1。
    段落标记删除写在 w:pPr/w:rPr/w:del，使该段与下一段合并。

    覆盖范围：run 可能是 <w:p> 的直接子节点，也可能嵌套在 <w:hyperlink> /
    <w:smartTag> / <w:sdt> 里（协议链接、内容控件）。只处理直接子节点会漏删，
    且仍返回 True → 上层以为修订已应用，实际链接文字原样留在正文。
    <w:tab/> 不是文本节点，删除时需一并搬进 <w:del>，否则残留的制表位会串到
    合并后的相邻段落。
    """
    from lxml import etree
    runs = _iter_paragraph_runs(p)
    if not runs:
        return False

    del_els = []
    for r in runs:
        text = _run_visible_text(r)
        tabs = _run_tab_count(r)
        brs = _run_br_count(r)
        d = _make_del(author, date_str, rev_ids.next())
        if text or not (tabs or brs):
            # 有文本：走常规路径（tabs/brs 一并补回）
            new_run = _clone_run_shell(r)
            _set_run_text(new_run, text, as_del_text=True, tabs=tabs, brs=brs)
        else:
            # 纯制表位/换行 run：只搬这些元素，不产生空文本节点
            new_run = etree.Element(_qn("w:r"))
            rpr = r.find(_qn("w:rPr"))
            if rpr is not None:
                new_run.append(copy.deepcopy(rpr))
            for _ in range(tabs):
                etree.SubElement(new_run, _qn("w:tab"))
            for _ in range(brs):
                etree.SubElement(new_run, _qn("w:br"))
        d.append(new_run)
        del_els.append((r, d))

    for r, d in del_els:
        r.getparent().replace(r, d)

    # 段落标记删除（合并到下一段）
    ppr = p.find(_qn("w:pPr"))
    if ppr is None:
        ppr = etree.Element(_qn("w:pPr"))
        p.insert(0, ppr)
    rpr = ppr.find(_qn("w:rPr"))
    if rpr is None:
        rpr = etree.Element(_qn("w:rPr"))
        # w:rPr 必须是 w:pPr 的最后一个子元素
        ppr.append(rpr)
    if rpr.find(_qn("w:del")) is None:
        # ⚠️ w:del 必须是 w:rPr 的第一个子元素（schema 顺序要求）
        rpr.insert(0, _make_del(author, date_str, rev_ids.next()))
    return True


def _make_paragraph_with_style(text, style_p, style_ppr, style_rpr,
                               author, date_str, rev_ids):
    """用预先取好的格式（pPr/rPr 深拷贝）构造新增段落，**返回段落列表**。

    与 _make_paragraph_like 的区别：格式在删除操作**之前**就已捕获，
    因此即使原段落已被包进 <w:del>，新增内容仍能继承正确的字体。

    text 中的 ``\\n`` 会拆成**多个真实 <w:p>**，而不是一个段落里的软换行：
    每个新段落各自克隆一份 pPr，从而保持居中/缩进/行距等段落格式。
    """
    from lxml import etree

    out = []
    for line in _split_insert_lines(text):
        new_p = etree.Element(_qn("w:p"))
        if style_ppr is not None:
            new_p.append(copy.deepcopy(style_ppr))
        elif style_p is not None:
            ppr = style_p.find(_qn("w:pPr"))
            if ppr is not None:
                new_p.append(copy.deepcopy(ppr))

        new_run = etree.Element(_qn("w:r"))
        if style_rpr is not None:
            new_run.append(copy.deepcopy(style_rpr))
        _set_run_text(new_run, line)

        ins = _make_ins(author, date_str, rev_ids.next())
        ins.append(new_run)
        new_p.append(ins)   # ⚠️ w:ins 是 w:p 的直接子节点（铁律 1）
        out.append(new_p)
    return out


def _split_paragraph_into(p, target, tjs, author, date_str, rev_ids) -> bool:
    """把原文一个段落（含软换行）拆成多个真实段落。

    用于「纯结构拆分」：原文把若干行写在同一段里（以 <w:br/> 软换行分隔），
    改写后这些行变成独立段落，文字本身没有变化。

    做法：
    - 第一个目标行保留在**原段落**里（内容不变，不产生修订）；
    - 其余目标行各建一个新段落，内容包在 <w:ins> 中；
    - 原段落中对应的 <w:br/> 与后续文字搬进新段落，
      原段落只保留第一行 —— 这样「段落数变化」本身就是可见的修订。

    要求原段落是纯 run 结构（由调用方保证）。
    """
    from lxml import etree

    if not tjs:
        return False

    src_ppr = p.find(_qn("w:pPr"))
    src_run = _first_run(p)
    src_rpr = src_run.find(_qn("w:rPr")) if src_run is not None else None

    # 目标行文本（跳过显式删除）
    lines = []
    for tj in tjs:
        kind, text = target[tj]
        if kind == "delete":
            return False
        for ln in _split_insert_lines(text):
            lines.append(ln)
    if len(lines) < 2:
        return False

    # 校验：各目标行拼起来必须与原段落文本一致（本函数只处理纯结构拆分）
    if "\n".join(lines) != _paragraph_text(p):
        return False

    # 原段落重建为「只含第一行」：保留原有 run，删掉第一个 <w:br/> 之后的内容
    seen_first_break = False
    for child in list(p):
        if child.tag == _qn("w:pPr"):
            continue
        if not seen_first_break:
            # 该 run 内可能含 <w:br/>：截断到第一个 break 之前
            brs = [c for c in child if c.tag == _qn("w:br")]
            if brs:
                first_br = brs[0]
                # 删掉 break 及其后所有同级子节点
                drop = False
                for c in list(child):
                    if c is first_br:
                        drop = True
                    if drop:
                        child.remove(c)
                seen_first_break = True
            continue
        # 第一个 break 之后的同级节点全部删除
        p.remove(child)

    # 为其余各行建新段落，插在原段落之后
    anchor = p
    for line in lines[1:]:
        new_p = etree.Element(_qn("w:p"))
        if src_ppr is not None:
            new_p.append(copy.deepcopy(src_ppr))
        new_run = etree.Element(_qn("w:r"))
        if src_rpr is not None:
            new_run.append(copy.deepcopy(src_rpr))
        _set_run_text(new_run, line)
        ins = _make_ins(author, date_str, rev_ids.next())
        ins.append(new_run)
        new_p.append(ins)   # ⚠️ w:ins 是 w:p 的直接子节点（铁律 1）
        anchor.addnext(new_p)
        anchor = new_p
    return True


def _make_paragraphs_from_template(template_p, target, tjs, author, date_str, rev_ids):
    """按模板段落格式，为**多出来的目标段落**构造新增段落。

    用于 `_try_inline_replace` 的「1 对多」场景：原文一段被拆成多段时，
    第一段的内容已由 `_replace_paragraph_inline` 就地细化，剩余目标段落
    必须作为**新增段落**补上，否则接受修订后这些内容会丢失。

    格式取自 template_p（即承载细化的那个原文段落），保证字体/缩进一致。
    """
    from lxml import etree

    src_ppr = template_p.find(_qn("w:pPr"))
    src_run = _first_run(template_p)
    src_rpr = src_run.find(_qn("w:rPr")) if src_run is not None else None

    out = []
    for tj in tjs:
        kind, text = target[tj]
        if kind == "delete":
            continue
        for line in _split_insert_lines(text):
            new_p = etree.Element(_qn("w:p"))
            if src_ppr is not None:
                new_p.append(copy.deepcopy(src_ppr))
            new_run = etree.Element(_qn("w:r"))
            if src_rpr is not None:
                new_run.append(copy.deepcopy(src_rpr))
            _set_run_text(new_run, line)
            ins = _make_ins(author, date_str, rev_ids.next())
            ins.append(new_run)
            new_p.append(ins)   # ⚠️ w:ins 是 w:p 的直接子节点（铁律 1）
            out.append(new_p)
    return out


def _split_insert_lines(text: str) -> list[str]:
    """把待新增的文本按行拆开（供新增段落使用）。

    换行一律视为**段落分隔**：新增内容本来就该是独立段落，不存在
    「新增一段却要在段内软换行」的语义。空行跳过。
    """
    if text is None:
        return []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return [ln for ln in lines if ln.strip()]


def _make_paragraph_like(anchor_p, text, kind, author, date_str, rev_ids):
    """基于锚点段落克隆出格式一致的新段落，内容包在 <w:ins> 中。

    **返回段落列表**：text 中的 ``\\n`` 拆成多个真实 <w:p>（见
    `_make_paragraph_with_style` 的说明）。
    """
    from lxml import etree
    if anchor_p is None:
        return []

    # 取格式模板 run；锚点段落没有直接 run 时（如仅含书签/域），
    # 退化为不带头格式的 run，避免整个新增段落丢失。
    src_run = _first_run(anchor_p)
    src_rpr = src_run.find(_qn("w:rPr")) if src_run is not None else None
    src_ppr = anchor_p.find(_qn("w:pPr"))

    out = []
    for line in _split_insert_lines(text):
        new_p = etree.Element(_qn("w:p"))
        # 复制段落格式 pPr
        if src_ppr is not None:
            new_p.append(copy.deepcopy(src_ppr))

        new_run = _clone_run_shell(src_run) if src_run is not None else etree.Element(_qn("w:r"))
        if new_run.find(_qn("w:rPr")) is None and src_rpr is not None:
            new_run.append(copy.deepcopy(src_rpr))
        _set_run_text(new_run, line)

        ins = _make_ins(author, date_str, rev_ids.next())
        ins.append(new_run)
        new_p.append(ins)   # ⚠️ w:ins 是 w:p 的直接子节点（铁律 1）
        out.append(new_p)
    return out