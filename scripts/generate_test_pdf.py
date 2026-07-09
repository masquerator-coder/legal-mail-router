"""Generate app/ocr_test_data.py with embedded test PDF + PNG.

Output: app/ocr_test_data.py — constants used at runtime by OCR config test.
"""
import fitz
import base64
import struct
import zlib
import os


def _make_png() -> bytes:
    """200x50 纯白 RGB PNG (~210 bytes)"""
    w, h = 200, 50
    def _chunk(ctype, data):
        c = ctype + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    header = b'\x89PNG\r\n\x1a\n'
    ihdr = _chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
    raw = b''
    for y in range(h):
        raw += b'\x00'
        for x in range(w):
            raw += b'\xff\xff\xff'
    idat = _chunk(b'IDAT', zlib.compress(raw))
    iend = _chunk(b'IEND', b'')
    return header + ihdr + idat + iend


def _make_pdf() -> bytes:
    """200x100 图片型 PDF（无文字层）, ~5.5KB, 内容 OCR-PDF-TEST-2024"""
    doc = fitz.open()
    page = doc.new_page(width=200, height=100)
    page.insert_text(fitz.Point(10, 55), "OCR-PDF-TEST-2024",
                     fontname="courier", fontsize=20)
    pix = page.get_pixmap(dpi=72)
    img_bytes = pix.tobytes("png")
    doc.close()
    doc2 = fitz.open()
    page2 = doc2.new_page(width=200, height=100)
    page2.insert_image(fitz.Rect(0, 0, 200, 100), stream=img_bytes)
    pdf_bytes = doc2.write(deflate_images=True)
    doc2.close()
    return pdf_bytes


def _wrap_b64(data: bytes, cols: int = 120) -> str:
    """Convert bytes to multi-line parenthesized Python string literal."""
    b64 = base64.b64encode(data).decode()
    assert len(b64) % 4 == 0
    lines = [f'    "{b64[i:i+cols]}"' for i in range(0, len(b64), cols)]
    return "(\n" + "\n".join(lines) + "\n)"


def generate():
    pdf_bytes = _make_pdf()
    png_bytes = _make_png()

    pdf_b64 = _wrap_b64(pdf_bytes)
    png_b64 = _wrap_b64(png_bytes)

    # ── Verify PDF ──
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    assert doc.page_count == 1 and not doc[0].get_text().strip()
    doc.close()

    source = f'''"""预制测试 PDF + PNG — 用于 OCR 配置页面的能力检测。

PDF: 图片型（无文字层），内容 "OCR-PDF-TEST-2024"
PNG: 200x50 白色图片

服务启动时解码一次，运行时复用。
生成自 scripts/generate_test_pdf.py。
"""
import base64

# ── 预制测试 PDF（图片型，无文字层） ──

PREBUILT_TEST_PDF_B64 = {pdf_b64}

PREBUILT_TEST_PDF: bytes = base64.b64decode(PREBUILT_TEST_PDF_B64)
"""图片型测试 PDF（约 {len(pdf_bytes)//1000}KB），无文字层。

预期行为:
  - MinerU 等 PDF-capable 服务: 返回文本包含 "OCR-PDF-TEST-2024"
  - PaddleOCR 等 image-only 服务: 返回错误或空文本
"""


# ── 预制测试 PNG（200x50 纯白图片） ──

PREBUILT_TEST_PNG_B64 = {png_b64}

PREBUILT_TEST_PNG: bytes = base64.b64decode(PREBUILT_TEST_PNG_B64)
"""测试用 PNG 图片（200x50 纯白，约 {len(png_bytes)} bytes），用于 OCR 连通性测试。
"""
'''

    target = os.path.join(os.path.dirname(__file__), "..", "app", "ocr_test_data.py")
    target = os.path.normpath(target)

    with open(target, "w", encoding="utf-8") as f:
        f.write(source)

    pdf_chars = len(base64.b64encode(pdf_bytes).decode())
    png_chars = len(base64.b64encode(png_bytes).decode())
    print(f"✅ PDF: {len(pdf_bytes)} bytes → {pdf_chars} b64 chars")
    print(f"✅ PNG: {len(png_bytes)} bytes → {png_chars} b64 chars")
    print(f"✅ Written: {target}")


if __name__ == "__main__":
    generate()
