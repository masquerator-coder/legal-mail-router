"""
压缩包附件解压模块

邮件收到 zip / rar / 7z / tar 等压缩包附件时，原本在文本提取阶段会被
当作「不支持的文件类型」跳过，导致 LLM 分析无内容而失败。本模块在
分析流程开始前将压缩包展开为普通附件（AttachmentInfo），使解压后的
文件进入完整的保存 → 分组 → 分析流程。

支持的格式：
  - zip / tar / tar.gz(tgz) / tar.bz2(tbz2) / tar.xz(txz) / gz / bz2 / xz
      —— Python 内置库，所有环境可用
  - rar / 7z
      —— 依赖外部命令（7z / unrar / unar），找不到时优雅降级（保留原附件）

安全防护：
  - zip-slip 防护：拒绝绝对路径、盘符路径、含 ``..`` 的成员名
  - 解压总量 / 单文件大小上限（防 zip bomb）
  - 解压文件数量上限
  - 嵌套压缩包递归展开深度上限
"""
from __future__ import annotations

import bz2
import gzip
import io
import logging
import lzma
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
import zlib
from pathlib import PurePosixPath

from app.services.email_fetcher import AttachmentInfo

logger = logging.getLogger(__name__)

# 支持的后缀（长后缀在前，先匹配 tar.gz 这类复合后缀）
_ARCHIVE_SUFFIXES = (
    ".tar.gz", ".tar.bz2", ".tar.xz",
    ".tgz", ".tbz2", ".txz",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
)

DEFAULT_MAX_DEPTH = 3          # 嵌套压缩包递归展开深度
DEFAULT_MAX_FILES = 100        # 单个压缩包最多展开文件数
DEFAULT_MAX_TOTAL_SIZE = 200 * 1024 * 1024   # 单压缩包解压总量上限 200MB
DEFAULT_MAX_FILE_SIZE = 100 * 1024 * 1024    # 单个解压文件大小上限 100MB

# 常见 7z / unrar / unar 可执行文件查找路径
_7Z_CANDIDATES = (
    "7z", "7za", "7zr",
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    "/usr/bin/7z", "/usr/bin/7za",
)
_UNRAR_CANDIDATES = (
    "unrar", "rar",
    r"C:\Program Files\WinRAR\UnRAR.exe",
    r"C:\Program Files\WinRAR\Rar.exe",
    "/usr/bin/unrar", "/usr/bin/rar",
)
_UNAR_CANDIDATES = ("unar", "/usr/bin/unar")


def is_archive(filename: str, content: bytes = b"") -> bool:
    """判断文件名（或 magic bytes）是否指向本模块支持的压缩包"""
    if not filename:
        return False
    lower = filename.lower()
    if any(lower.endswith(sfx) for sfx in _ARCHIVE_SUFFIXES):
        return True
    # 扩展名优先：OOXML（docx/xlsx/pptx）等 zip 容器是文档格式，不是压缩包
    if any(lower.endswith(sfx) for sfx in _ZIP_CONTAINER_EXTS):
        return False
    # 扩展名兜底：按 magic bytes 识别
    if content:
        if content[:2] == b"PK":
            # zip 家族：OOXML 文档（含 [Content_Types].xml）不是压缩包
            return not _looks_like_ooxml(content)
        if content[:2] == b"\x1f\x8b":
            return True  # gzip
        if content[:3] == b"BZh":
            return True  # bzip2
        if content[:6] == b"\xfd7zXZ\x00":
            return True  # xz
        if content[:8] == b"\x37\x7a\xbc\xaf\x27\x1c":
            return True  # 7z
        if content[:7] in (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00"):
            return True  # rar (rar4 / rar5)
    return False


# 以 zip 为容器但属于文档/应用的格式（OOXML 办公文档、OpenDocument、EPUB 等），
# 内部是结构化文件而非「附件包」，解压会破坏其可读性，故不视为压缩包。
_ZIP_CONTAINER_EXTS = (
    # Office Open XML（docx / xlsx / pptx 及其模板/宏变体）
    ".docx", ".docm", ".dotx", ".dotm",
    ".xlsx", ".xlsm", ".xltx", ".xltm",
    ".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm",
    # OpenDocument
    ".odt", ".ods", ".odp",
    # 其他常见 zip 容器
    ".epub", ".jar", ".vsdx",
)


def _looks_like_ooxml(content: bytes) -> bool:
    """检查 zip 内容是否为 OOXML 文档（OOXML 压缩包必含 [Content_Types].xml）"""
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            return "[Content_Types].xml" in zf.namelist()
    except Exception:
        return False


def _strip_archive_suffix(filename: str) -> str:
    """去掉压缩后缀，得到解压后文件的默认名（如 xxx.pdf.gz → xxx.pdf）"""
    lower = filename.lower()
    for sfx in _ARCHIVE_SUFFIXES:
        if lower.endswith(sfx):
            return filename[: -len(sfx)]
    return filename


def _safe_member_name(raw_name: str) -> str | None:
    """
    校验压缩包成员名，返回安全文件名；危险成员（路径穿越 / 绝对路径）返回 None。
    zip-slip 防护：规范化后不允许出现 '..' 组件、绝对路径或盘符。
    """
    if not raw_name:
        return None
    # zip 中可能出现反斜杠分隔（Windows 打包），统一为 '/'
    name = raw_name.replace("\\", "/")
    # 绝对路径（/xxx）与盘符路径（C:/xxx）
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = PurePosixPath(name).parts
    if any(p == ".." for p in parts):
        return None
    # 只取文件名部分，避免子目录结构引入复杂路径
    base = PurePosixPath(name).name
    if not base or base in (".", ".."):
        return None
    return base


def _mime_for_filename(filename: str) -> str:
    """按扩展名给出粗略 MIME 类型（仅用于记录，分析流程主要依据扩展名）"""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime_map = {
        "pdf": "application/pdf", "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xls": "application/vnd.ms-excel",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "txt": "text/plain", "md": "text/markdown", "csv": "text/csv",
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "bmp": "image/bmp", "tiff": "image/tiff", "webp": "image/webp",
    }
    return mime_map.get(ext, "application/octet-stream")


class _ExtractionBudget:
    """解压预算：文件数量 / 单文件大小 / 总量上限"""

    def __init__(self, max_files: int, max_file_size: int, max_total_size: int):
        self.max_files = max_files
        self.max_file_size = max_file_size
        self.max_total_size = max_total_size
        self.count = 0
        self.total = 0

    def check(self, file_size: int) -> bool:
        """返回 False 表示超出预算"""
        if self.count >= self.max_files:
            return False
        if file_size > self.max_file_size:
            return False
        if self.total + file_size > self.max_total_size:
            return False
        return True

    def consume(self, size: int) -> None:
        self.count += 1
        self.total += size


def _read_limited(stream, budget) -> bytes | None:
    """
    流式读取解压成员，限制单文件大小；返回 bytes。
    超过限制时返回 None（调用方跳过该成员，避免 zip bomb 占用内存）。
    """
    data = bytearray()
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > budget.max_file_size:
            return None
    return bytes(data)


def _extract_zip(content: bytes, budget: _ExtractionBudget) -> list[tuple[str, bytes]]:
    """解压 zip（内存流），返回 [(安全文件名, bytes)]"""
    result: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            safe_name = _safe_member_name(info.filename)
            if safe_name is None:
                logger.warning(f"zip 成员名存在路径穿越风险，已跳过: {info.filename!r}")
                continue
            # 预检查声明大小（快速拒绝明显超限；实际大小在流式读取时再次校验）
            if not budget.check(info.file_size):
                logger.warning(f"zip 成员超出解压预算，已跳过: {info.filename}")
                continue
            try:
                with zf.open(info) as f:
                    data = _read_limited(f, budget)
            except (zipfile.BadZipFile, RuntimeError, OSError) as e:
                logger.warning(f"zip 成员读取失败: {info.filename} — {e}")
                continue
            if data is None:
                logger.warning(f"zip 成员实际大小超限，已跳过: {info.filename}")
                continue
            if not budget.check(len(data)):
                continue
            budget.consume(len(data))
            result.append((safe_name, data))
    return result


def _extract_tar(content: bytes, budget: _ExtractionBudget) -> list[tuple[str, bytes]]:
    """解压 tar / tar.gz / tar.bz2 / tar.xz（内存流，自动识别压缩模式）"""
    result: list[tuple[str, bytes]] = []
    try:
        tf = tarfile.open(fileobj=io.BytesIO(content), mode="r:*")
    except tarfile.TarError as e:
        logger.warning(f"tar 解析失败: {e}")
        return result
    with tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            safe_name = _safe_member_name(member.name)
            if safe_name is None:
                logger.warning(f"tar 成员名存在路径穿越风险，已跳过: {member.name!r}")
                continue
            if not budget.check(member.size):
                logger.warning(f"tar 成员超出解压预算，已跳过: {member.name}")
                continue
            try:
                f = tf.extractfile(member)
                if f is None:
                    continue
                data = _read_limited(f, budget)
                f.close()
            except (tarfile.TarError, OSError) as e:
                logger.warning(f"tar 成员读取失败: {member.name} — {e}")
                continue
            if data is None:
                logger.warning(f"tar 成员实际大小超限，已跳过: {member.name}")
                continue
            budget.consume(len(data))
            result.append((safe_name, data))
    return result


def _decompress_limited(make_decompressor, content: bytes, max_size: int) -> bytes | None:
    """
    流式解压单文件流（gz / bz2 / xz），解压结果超过 max_size 时中止。
    每次 decompress 均以 max_length 限制单次输出，避免高压缩比输入块
    一次性分配巨量内存（zip bomb）；返回 bytes，超限或失败返回 None。
    """
    d = make_decompressor()
    out = bytearray()
    remaining = content
    while remaining:
        chunk, remaining = remaining[:65536], remaining[65536:]
        try:
            # 单次输出限制：即使单个输入块压缩比极高，也不会瞬时分配超限内存
            out.extend(d.decompress(chunk, max_length=max(1, max_size - len(out) + 1)))
        except Exception:
            return None
        if len(out) > max_size:
            return None
        # 输入块未消费完 → 输出已超限（zlib 为 unconsumed_tail，bz2/lzma 为 unused_data）
        unconsumed = getattr(d, "unconsumed_tail", None) or getattr(d, "unused_data", None)
        if unconsumed:
            return None
    try:
        out.extend(d.flush())
    except Exception:
        return None
    if len(out) > max_size:
        return None
    return bytes(out)


def _make_gzip_decompressor():
    """返回解压 gzip 流的 zlib decompressobj（gzip 模块无 decompressobj API）"""
    return zlib.decompressobj(16 + zlib.MAX_WBITS)


def _extract_single_stream(
    content: bytes,
    filename: str,
    budget: _ExtractionBudget,
    make_decompressor,
) -> list[tuple[str, bytes]]:
    """解压单文件流压缩（gz / bz2 / xz），解压结果以去掉压缩后缀命名"""
    try:
        data = _decompress_limited(make_decompressor, content, budget.max_file_size)
    except Exception as e:
        logger.warning(f"解压 {filename} 失败: {e}")
        return []
    if data is None:
        logger.warning(f"解压结果超过单文件上限或解压失败，已跳过: {filename}")
        return []
    if not budget.check(len(data)):
        logger.warning(f"解压结果超出预算，已跳过: {filename}")
        return []
    budget.consume(len(data))
    base = _strip_archive_suffix(filename) or filename
    return [(base, data)]


def _find_tool(candidates) -> str | None:
    for cand in candidates:
        if os.path.sep in cand or "/" in cand or "\\" in cand:
            if os.path.exists(cand):
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    return None


def _parse_listed_total_size(list_output: str) -> int | None:
    """
    从 7z/unrar/lsar 的 list 输出解析「未压缩总字节数」。
    7z 与 unrar 的列表尾部均有 'N files, M bytes' 形式的总计行；
    7z -slt 技术输出为逐文件 'Size = N'；lsar 为逐行 'name - N bytes'。
    无法解析返回 None。
    """
    # 尾部总计行（7z / unrar 通用格式），取最后一个字节数（= 未压缩总量）
    sizes = re.findall(r"(\d+)\s+bytes?\b", list_output)
    if sizes:
        # lsar 是逐文件 'name - N bytes'，不能取最后一个，需求和；
        # 7z/unrar 是总计行，取最后一个即可。
        if list_output.count("bytes") > 1 and re.search(r"-\s*\d+\s+bytes", list_output):
            return sum(int(s) for s in sizes)
        return int(sizes[-1])
    # 7z -slt 技术输出：逐文件 Size = N 求和
    total = 0
    found = False
    for line in list_output.splitlines():
        m = re.match(r"\s*Size\s*=\s*(\d+)", line)
        if m:
            total += int(m.group(1))
            found = True
    return total if found else None


def _extract_with_external(
    content: bytes,
    filename: str,
    budget: _ExtractionBudget,
) -> list[tuple[str, bytes]]:
    """通过外部命令（7z / unrar / unar）解压 rar / 7z，返回 [(安全文件名, bytes)]"""
    lower = filename.lower()
    tool = None
    if lower.endswith(".rar"):
        # p7zip 的 7z 不含 RAR 解码器，故 unar（支持 RAR5）优先于 7z
        tool = _find_tool(_UNRAR_CANDIDATES) or _find_tool(_UNAR_CANDIDATES) or _find_tool(_7Z_CANDIDATES)
    elif lower.endswith(".7z"):
        tool = _find_tool(_7Z_CANDIDATES) or _find_tool(_UNAR_CANDIDATES)
    if not tool:
        logger.warning(
            f"未找到可用的解压工具（7z/unrar/unar），无法解压 {filename}，"
            f"将按原附件保留（不参与文本分析）"
        )
        return []

    tmp_in = tempfile.NamedTemporaryFile(prefix="lmail_arc_", suffix=os.path.splitext(filename)[1] or ".bin", delete=False)
    try:
        tmp_in.write(content)
        tmp_in.close()

        # ── 解压前预检：核对未压缩总大小，超限直接拒绝 ──
        # 防止 7z/rar bomb 先解压到临时目录耗尽磁盘（解析失败则跳过预检，由解压后校验兜底）
        try:
            tool_name = os.path.basename(tool).lower()
            # unar 不支持 list 子命令，其配套 lsar 负责列目录
            list_cmd = [tool.replace("unar", "lsar"), tmp_in.name] if "unar" in tool_name else [tool, "l", tmp_in.name]
            proc = subprocess.run(list_cmd, capture_output=True, timeout=60)
            if proc.returncode == 0:
                text = proc.stdout.decode("utf-8", errors="replace")
                listed_total = _parse_listed_total_size(text)
                if listed_total is not None and not budget.check(listed_total):
                    logger.warning(
                        f"压缩包 {filename} 解压总量 {listed_total} 字节超出预算"
                        f"（{budget.max_total_size}），已跳过解压"
                    )
                    return []
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug(f"压缩包总量预检失败（跳过预检）: {e}")

        tmp_out = tempfile.mkdtemp(prefix="lmail_arc_out_")
        try:
            tool_name = os.path.basename(tool).lower()
            if "unar" in tool_name:
                cmd = [tool, "-q", "-o", tmp_out, tmp_in.name]
            elif "7z" in tool_name or "7za" in tool_name or "7zr" in tool_name:
                cmd = [tool, "x", "-y", f"-o{tmp_out}", tmp_in.name]
            else:  # unrar / rar
                cmd = [tool, "x", "-y", tmp_in.name, f"{tmp_out}{os.path.sep}"]
            proc = subprocess.run(cmd, capture_output=True, timeout=120)
            if proc.returncode != 0:
                logger.warning(
                    f"外部解压失败 {filename}: {tool} 返回码 {proc.returncode} — "
                    f"{proc.stderr.decode('utf-8', errors='replace')[:200]}"
                )
                return []
            result: list[tuple[str, bytes]] = []
            for root, _, files in os.walk(tmp_out):
                for f in files:
                    full = os.path.join(root, f)
                    # 拒绝符号链接：可能指向解压目录外的文件
                    if os.path.islink(full):
                        logger.warning(f"解压输出为符号链接，已跳过: {f}")
                        continue
                    rel = os.path.relpath(full, tmp_out)
                    safe_name = _safe_member_name(rel)
                    if safe_name is None:
                        logger.warning(f"解压输出存在路径穿越风险，已跳过: {rel!r}")
                        continue
                    size = os.path.getsize(full)
                    if not budget.check(size):
                        logger.warning(f"解压输出超出预算，已跳过: {rel}")
                        continue
                    with open(full, "rb") as fh:
                        data = fh.read()
                    budget.consume(len(data))
                    result.append((safe_name, data))
            return result
        finally:
            shutil.rmtree(tmp_out, ignore_errors=True)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(f"外部解压异常 {filename}: {e}")
        return []
    finally:
        try:
            os.unlink(tmp_in.name)
        except OSError:
            pass


def _expand_one(
    attachment: AttachmentInfo,
    depth: int,
    max_depth: int,
    budget: _ExtractionBudget,
    stats: dict,
) -> list[AttachmentInfo]:
    """展开单个压缩包附件（递归），返回解压出的附件列表；非压缩包原样返回"""
    filename = attachment.filename or "unknown"
    content = attachment.content or b""
    if not is_archive(filename, content):
        return [attachment]

    if depth > max_depth:
        logger.warning(f"压缩包嵌套超过 {max_depth} 层，{filename} 不再继续展开（保留原附件）")
        return [attachment]

    ext = filename.lower()
    try:
        if ext.endswith(".zip") or content[:2] == b"PK":
            entries = _extract_zip(content, budget)
        elif ext.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar")):
            entries = _extract_tar(content, budget)
        elif ext.endswith(".gz"):
            entries = _extract_single_stream(content, filename, budget, _make_gzip_decompressor)
        elif ext.endswith(".bz2"):
            entries = _extract_single_stream(content, filename, budget, lambda: bz2.BZ2Decompressor())
        elif ext.endswith(".xz"):
            entries = _extract_single_stream(content, filename, budget, lambda: lzma.LZMADecompressor())
        elif ext.endswith((".rar", ".7z")):
            entries = _extract_with_external(content, filename, budget)
        else:
            # magic bytes 命中但扩展名未知 → 按内容特征尝试
            if content[:2] == b"\x1f\x8b":
                entries = _extract_single_stream(content, filename, budget, _make_gzip_decompressor)
            elif content[:3] == b"BZh":
                entries = _extract_single_stream(content, filename, budget, lambda: bz2.BZ2Decompressor())
            elif content[:6] == b"\xfd7zXZ\x00":
                entries = _extract_single_stream(content, filename, budget, lambda: lzma.LZMADecompressor())
            else:
                logger.warning(f"无法识别压缩格式: {filename}")
                return [attachment]
    except Exception as e:
        logger.error(f"解压 {filename} 失败: [{type(e).__name__}] {e}")
        stats.setdefault("errors", []).append(f"{filename}: {e}")
        return [attachment]

    if not entries:
        # 解压成功但没有得到任何文件（空包 / 全被预算拦截）→ 保留原附件
        stats.setdefault("errors", []).append(f"{filename}: 解压结果为空或全部被安全限制拦截")
        return [attachment]

    stats["expanded"] = True
    stats["extracted"] += len(entries)

    expanded: list[AttachmentInfo] = []
    for safe_name, data in entries:
        inner = AttachmentInfo(
            filename=safe_name,
            content=data,
            content_type=_mime_for_filename(safe_name),
        )
        if is_archive(safe_name, data):
            # 嵌套压缩包：递归展开（深度 +1）
            expanded.extend(_expand_one(inner, depth + 1, max_depth, budget, stats))
        else:
            expanded.append(inner)
    return expanded


def expand_archive_attachments(
    attachments: list[AttachmentInfo],
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_files: int = DEFAULT_MAX_FILES,
    max_total_size: int = DEFAULT_MAX_TOTAL_SIZE,
    max_file_size: int = DEFAULT_MAX_FILE_SIZE,
) -> tuple[list[AttachmentInfo], dict]:
    """
    将附件列表中的压缩包展开为普通附件。

    原始压缩包在解压成功后由解压出的文件替代（避免无文本的压缩包干扰后续
    分组/分析）；解压失败或缺少外部工具时保留原压缩包附件，按原逻辑处理。

    返回: (展开后的附件列表, 统计信息)
          stats: {"expanded": bool, "extracted": int, "errors": list[str]}
    """
    budget = _ExtractionBudget(max_files, max_file_size, max_total_size)
    stats = {"expanded": False, "extracted": 0, "errors": []}
    result: list[AttachmentInfo] = []
    for att in attachments:
        result.extend(_expand_one(att, depth=1, max_depth=max_depth, budget=budget, stats=stats))
    return result, stats
