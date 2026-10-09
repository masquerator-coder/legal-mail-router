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

    # 构造目标段落序列：按行拆分，但保留行内换行的语义
    # （标记块内若含空行，视为段落分隔；单换行视为段内软换行）
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


def _split_target_paragraphs(content: str) -> list[str]:
    """把一段标记内容拆成目标段落。

    仅按**空行**（连续两个及以上换行）拆分段落；单个换行保留为
    段内软换行（与原文书常见的 ``甲方：…\\n乙方：…`` 结构一致）。
    """
    if content is None:
        return []
    # 统一换行符
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    # 用空行拆段
    parts = re.split(r"\n[ \t\u3000]*\n+", text)
    return [p for p in parts]


def _diff_apply(entries, orig_texts, target, author, date_str, rev_ids) -> int:
    """基于 LCS 的段落级差异应用（保证格式继承自相邻原文段落）"""
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
            # 原文段落删除 + 新段落插入。
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
                newp = _make_paragraph_with_style(
                    target[j][1], style_p, style_ppr, style_rpr,
                    author, date_str, rev_ids
                )
                if newp is not None and insert_after is not None:
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
                newp = _make_paragraph_like(
                    style_anchor, target[j][1], kind, author, date_str, rev_ids
                )
                if newp is None:
                    continue
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
    """用预先取好的格式（pPr/rPr 深拷贝）构造新增段落。

    与 _make_paragraph_like 的区别：格式在删除操作**之前**就已捕获，
    因此即使原段落已被包进 <w:del>，新增内容仍能继承正确的字体。
    """
    from lxml import etree

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
    _set_run_text(new_run, text)

    ins = _make_ins(author, date_str, rev_ids.next())
    ins.append(new_run)
    new_p.append(ins)   # ⚠️ w:ins 是 w:p 的直接子节点（铁律 1）
    return new_p


def _make_paragraph_like(anchor_p, text, kind, author, date_str, rev_ids):
    """基于锚点段落克隆出格式一致的新段落，内容包在 <w:ins> 中。"""
    from lxml import etree
    if anchor_p is None:
        return None

    new_p = etree.Element(_qn("w:p"))
    # 复制段落格式 pPr
    ppr = anchor_p.find(_qn("w:pPr"))
    if ppr is not None:
        new_p.append(copy.deepcopy(ppr))

    # 取格式模板 run；锚点段落没有直接 run 时（如仅含书签/域），
    # 退化为不带头格式的 run，避免整个新增段落丢失。
    src_run = _first_run(anchor_p)
    new_run = _clone_run_shell(src_run) if src_run is not None else etree.Element(_qn("w:r"))
    _set_run_text(new_run, text)

    ins = _make_ins(author, date_str, rev_ids.next())
    ins.append(new_run)
    new_p.append(ins)   # ⚠️ w:ins 是 w:p 的直接子节点（铁律 1）
    return new_p