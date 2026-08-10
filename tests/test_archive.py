"""
Archive — 压缩包附件解压模块测试
"""
# -*- coding: utf-8 -*-
import io
import tarfile
import zipfile

import pytest

from app.services.archive import (
    expand_archive_attachments,
    is_archive,
    _safe_member_name,
)
from app.services.email_fetcher import AttachmentInfo


def _att(filename, content):
    return AttachmentInfo(filename=filename, content=content, content_type="application/octet-stream")


def _build_deceptive_zip(real_size_bytes: int, claimed_size: int) -> bytes:
    """
    构造「声明大小与实际不符」的 zip：成员实际解压出 real_size_bytes 字节，
    但 Central Directory 中记录的 uncompressed size 为 claimed_size（更小）。
    用于验证解压不受声明值欺骗（zip bomb 防护）。
    """
    import struct

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("payload.bin", b"\x00" * real_size_bytes)
    data = bytearray(buf.getvalue())
    # Central Directory 条目中 uncompressed size 位于偏移 +28（4 字节）
    pos = 0
    while True:
        idx = data.find(b"PK\x01\x02", pos)
        if idx == -1:
            break
        struct.pack_into("<I", data, idx + 28, claimed_size)
        pos = idx + 4
    return bytes(data)


def _make_zip(members: dict, filename="case.zip") -> AttachmentInfo:
    """members: {内部文件名: bytes}"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return _att(filename, buf.getvalue())


class TestIsArchive:
    def test_zip_by_ext(self):
        assert is_archive("documents.zip")

    def test_rar_7z_by_ext(self):
        assert is_archive("a.rar")
        assert is_archive("a.7z")

    def test_tar_gz_compound(self):
        assert is_archive("a.tar.gz")
        assert is_archive("a.tgz")

    def test_normal_file_not_archive(self):
        assert not is_archive("起诉状.pdf")
        assert not is_archive("合同.docx")

    def test_zip_by_magic(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("a.txt", "x")
        assert is_archive("weird-name-no-ext", buf.getvalue())

    def test_docx_not_archive(self):
        """docx 是 zip 容器，必须按文档处理而非压缩包（防止解压成内部 XML）"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("[Content_Types].xml", "<xml/>")
            zf.writestr("word/document.xml", "<w:document/>")
        data = buf.getvalue()
        assert is_archive("合同.docx", data) is False
        assert is_archive("AI分析报告.docx", data) is False
        # 无扩展名也能通过内容识别
        assert is_archive("合同文件", data) is False

    def test_xlsx_pptx_not_archive(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("[Content_Types].xml", "<xml/>")
        data = buf.getvalue()
        assert is_archive("表格.xlsx", data) is False
        assert is_archive("演示.pptx", data) is False

    def test_docx_expand_kept_untouched(self):
        """docx 附件展开时应原样保留，不产生内部 XML 文件"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("[Content_Types].xml", "<xml/>")
            zf.writestr("word/document.xml", "<w:document/>")
        att = AttachmentInfo(filename="合同.docx", content=buf.getvalue(), content_type="application/vnd")
        expanded, stats = expand_archive_attachments([att])
        assert stats["expanded"] is False
        assert len(expanded) == 1
        assert expanded[0].filename == "合同.docx"

    def test_zip_inside_docx_still_expanded(self):
        """真实压缩包仍正常展开（确保修复不误伤 zip 附件）"""
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as zf:
            zf.writestr("证据.pdf", b"%PDF-1.4")
        att = AttachmentInfo(filename="材料.zip", content=inner.getvalue(), content_type="application/zip")
        expanded, stats = expand_archive_attachments([att])
        assert stats["expanded"] is True
        assert "证据.pdf" in [a.filename for a in expanded]


class TestSafeMemberName:
    def test_plain(self):
        assert _safe_member_name("起诉状.pdf") == "起诉状.pdf"

    def test_subdir_flattened(self):
        assert _safe_member_name("docs/合同.docx") == "合同.docx"

    def test_path_traversal_rejected(self):
        assert _safe_member_name("../evil.txt") is None
        assert _safe_member_name("a/../../evil.txt") is None

    def test_absolute_rejected(self):
        assert _safe_member_name("/etc/passwd") is None
        assert _safe_member_name("C:/Windows/evil.exe") is None

    def test_backslash_normalized(self):
        assert _safe_member_name("sub\\doc.docx") == "doc.docx"
        assert _safe_member_name("..\\evil.txt") is None

    def test_empty_rejected(self):
        assert _safe_member_name("") is None


class TestExpandZip:
    def test_basic_zip(self):
        att = _make_zip({"起诉状.pdf": b"%PDF-1.4 fake", "证据清单.txt": "清单内容".encode("utf-8")})
        expanded, stats = expand_archive_attachments([att])
        assert stats["expanded"] is True
        assert stats["extracted"] == 2
        # 解压文件替代原始压缩包
        assert len(expanded) == 2
        names = [a.filename for a in expanded]
        assert "起诉状.pdf" in names
        assert "证据清单.txt" in names

    def test_zip_slip_member_skipped(self):
        att = _make_zip({"../evil.txt": b"pwned", "safe.txt": b"ok"})
        expanded, stats = expand_archive_attachments([att])
        names = [a.filename for a in expanded]
        assert "safe.txt" in names
        assert "evil.txt" not in names
        assert not any(".." in n for n in names)

    def test_nested_zip_recursed(self):
        inner = _make_zip({"内层文书.pdf": b"%PDF inner"}, filename="inner.zip")
        outer = _make_zip({"inner.zip": inner.content})
        expanded, stats = expand_archive_attachments([outer])
        names = [a.filename for a in expanded]
        # 嵌套 zip 递归展开，最内层文件直接可用
        assert "inner.zip" not in names
        assert "内层文书.pdf" in names

    def test_max_depth_respected(self):
        # 构造 5 层嵌套 zip
        data = _make_zip({"leaf.txt": b"leaf"}).content
        for _ in range(4):
            data = _make_zip({"nest.zip": data}).content
        att = _att("deep.zip", data)
        expanded, stats = expand_archive_attachments([att])
        names = [a.filename for a in expanded]
        # 深度限制为 3，leaf.txt 不应被展开出来
        assert "leaf.txt" not in names

    def test_file_count_limit(self):
        att = _make_zip({f"f{i}.txt": b"x" for i in range(150)})
        expanded, stats = expand_archive_attachments([att], max_files=50)
        assert len(expanded) <= 50

    def test_size_limit(self):
        att = _make_zip({"big.bin": b"x" * (1024 * 1024)})
        expanded, stats = expand_archive_attachments([att], max_file_size=1024)
        assert len(expanded) == 1  # 仅原始压缩包，大文件被拦截

    def test_corrupt_zip_kept(self):
        att = _att("broken.zip", b"not a real zip at all")
        expanded, stats = expand_archive_attachments([att])
        # 损坏压缩包不崩溃，原附件保留
        assert len(expanded) == 1
        assert expanded[0].filename == "broken.zip"
        assert stats["expanded"] is False
        assert stats["errors"]

    def test_deceptive_declared_size_bounded(self):
        """声明 100 字节、实际 2MB 的 zip：流式读取必须拦截，不被声明值欺骗"""
        att = _att("trap.zip", _build_deceptive_zip(2 * 1024 * 1024, 100))
        expanded, stats = expand_archive_attachments([att], max_file_size=1024 * 1024)
        names = [a.filename for a in expanded]
        assert "payload.bin" not in names
        # 全部成员被拦截 → 保留原压缩包
        assert any(n == "trap.zip" for n in names)

    def test_deceptive_declared_size_under_budget_ok(self):
        """声明与实际一致且未超限时正常解压（确保流式限制不误伤）"""
        att = _att("ok.zip", _build_deceptive_zip(1024, 1024))
        expanded, stats = expand_archive_attachments([att], max_file_size=1024 * 1024)
        names = [a.filename for a in expanded]
        assert "payload.bin" in names


class TestExpandTar:
    def test_tar_gz(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.addfile(
                tarfile.TarInfo("判决书.txt"),
                io.BytesIO(b"\xe5\x88\xa4\xe5\x86\xb3\xe4\xb9\xa6\xe5\x86\x85\xe5\xae\xb9"),
            )
        att = _att("材料.tar.gz", buf.getvalue())
        expanded, stats = expand_archive_attachments([att])
        names = [a.filename for a in expanded]
        assert "判决书.txt" in names
        assert stats["extracted"] == 1

    def test_tar_slip_rejected(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo("../evil.txt")
            tf.addfile(info, io.BytesIO(b"x"))
        att = _att("材料.tar", buf.getvalue())
        expanded, stats = expand_archive_attachments([att])
        assert not any(".." in a.filename for a in expanded)


class TestSingleStream:
    def test_gz_single_file(self):
        import gzip

        data = gzip.compress(b"\xe5\x90\x88\xe5\x90\x8c\xe6\xad\xa3\xe6\x96\x87")  # 合同正文
        att = _att("合同正文.txt.gz", data)
        expanded, stats = expand_archive_attachments([att])
        names = [a.filename for a in expanded]
        assert "合同正文.txt" in names
        assert stats["extracted"] == 1

    def test_gz_unknown_ext_by_magic(self):
        import gzip

        data = gzip.compress(b"content")
        att = _att("weird.bin", data)
        expanded, stats = expand_archive_attachments([att])
        assert stats["expanded"] is True

    def test_gz_bomb_limited(self):
        """gz 压缩炸弹：2MB 内容压缩后很小，解压必须被单文件上限拦截"""
        import gzip

        bomb = gzip.compress(b"\x00" * (2 * 1024 * 1024))
        att = _att("bomb.gz", bomb)
        expanded, stats = expand_archive_attachments([att], max_file_size=1024 * 1024)
        # 超限 → 中止解压 → 保留原附件
        assert len(expanded) == 1
        assert expanded[0].filename == "bomb.gz"


class TestMixedAndPassthrough:
    def test_mixed_with_normal_attachments(self):
        pdf = _att("起诉状.pdf", b"%PDF-1.4")
        zip_att = _make_zip({"证据.xlsx": b"xlsx"})
        expanded, stats = expand_archive_attachments([pdf, zip_att])
        # pdf 保留 + zip 被解压文件替代
        assert len(expanded) == 2
        assert expanded[0].filename == "起诉状.pdf"
        assert stats["expanded"] is True

    def test_no_archive_unchanged(self):
        atts = [_att("a.pdf", b"%PDF"), _att("b.docx", b"docx")]
        expanded, stats = expand_archive_attachments(atts)
        assert stats["expanded"] is False
        assert expanded == atts

    def test_empty_list(self):
        expanded, stats = expand_archive_attachments([])
        assert expanded == []
        assert stats["expanded"] is False


def _find_7z():
    from app.services.archive import _find_tool, _7Z_CANDIDATES
    return _find_tool(_7Z_CANDIDATES)


def _find_unrar():
    from app.services.archive import _find_tool, _UNRAR_CANDIDATES
    return _find_tool(_UNRAR_CANDIDATES)


class TestParseListedTotalSize:
    """外部 list 输出总量解析"""

    def test_7z_summary_line(self):
        from app.services.archive import _parse_listed_total_size
        out = "Type = 7z\n--------------------\n5 files, 12345 bytes\n4 folders\n"
        assert _parse_listed_total_size(out) == 12345

    def test_unrar_summary_line(self):
        from app.services.archive import _parse_listed_total_size
        out = ("----  ----------  ----------  ----------\n"
               "File  1000  500  a.txt\n"
               "----  ----------  ----------  ----------\n"
               "2 files, 2500 bytes")
        assert _parse_listed_total_size(out) == 2500

    def test_lsar_per_line_sum(self):
        from app.services.archive import _parse_listed_total_size
        out = ("big.bin - 2097152 bytes\n"
               "small.txt - 100 bytes\n"
               "dir/\n"
               "  inner.pdf - 4096 bytes")
        assert _parse_listed_total_size(out) == 2097152 + 100 + 4096

    def test_unparseable_returns_none(self):
        from app.services.archive import _parse_listed_total_size
        assert _parse_listed_total_size("total garbage output") is None


class TestExternalTools:
    """rar / 7z 依赖外部命令，无工具时优雅降级"""

    @pytest.mark.skipif(not (_find_7z() or _find_unrar()), reason="需要 7z/unrar 外部命令")
    def test_7z_with_tool(self):
        # 用 7z 创建一个 7z 包并解压（有工具才跑）
        import shutil
        import subprocess
        import tempfile
        import os

        tool = _find_7z()
        if not tool:
            pytest.skip("无 7z 工具")
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "证据.txt")
            with open(src, "w", encoding="utf-8") as f:
                f.write("证据内容")
            arc = os.path.join(td, "case.7z")
            subprocess.run([tool, "a", arc, src], capture_output=True, check=True)
            with open(arc, "rb") as f:
                data = f.read()
        att = _att("case.7z", data)
        expanded, stats = expand_archive_attachments([att])
        names = [a.filename for a in expanded]
        assert "证据.txt" in names

    @pytest.mark.skipif(not _find_7z(), reason="需要 7z 外部命令")
    def test_7z_total_size_precheck(self):
        """7z bomb：解压前按 list 总量预检拦截，避免先写盘再拦截"""
        import subprocess
        import tempfile
        import os

        tool = _find_7z()
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "big.bin")
            with open(src, "wb") as f:
                f.write(b"\x00" * (2 * 1024 * 1024))
            arc = os.path.join(td, "case.7z")
            subprocess.run([tool, "a", arc, src], capture_output=True, check=True)
            with open(arc, "rb") as f:
                data = f.read()
        att = _att("case.7z", data)
        # 总量上限 1MB < 实际 2MB → 解压前被预检拦截，保留原附件
        expanded, stats = expand_archive_attachments([att], max_total_size=1024 * 1024)
        names = [a.filename for a in expanded]
        assert "big.bin" not in names
        assert "case.7z" in names

    def test_rar_without_tool_degrades(self):
        """无工具时：保留原附件、不崩溃、记录错误"""
        # 伪造一个无法被内置库识别的 rar 头，走外部命令路径
        fake_rar = b"Rar!\x1a\x07\x00" + b"\x00" * 64
        att = _att("case.rar", fake_rar)
        expanded, stats = expand_archive_attachments([att])

