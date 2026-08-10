"""
邮件拉取模块 — 支持多种邮箱服务商
"""
import re
import ssl
import socket
import email
import hashlib
import logging
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field

from app.config import decrypt, ATTACHMENTS_DIR

logger = logging.getLogger(__name__)

# IMAP 月份映射（确保英文月份）
_IMAP_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _imap_astring(value: str) -> str:
    """RFC 3501 4.3: 将任意字符串转为 IMAP ASTRING 格式（双引号字符串，转义 \\ 和 \"）"""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass
class ParsedEmail:
    """解析后的邮件"""
    message_id: str
    subject: str
    sender: str
    date: datetime
    body_text: str
    recipient: str = ""  # 收件人（To + Cc，Bcc 收到时回退投递地址）
    attachments: list = field(default_factory=list)
    headers: dict = field(default_factory=dict)  # 原始邮件头（关键头部的键值对）


@dataclass
class AttachmentInfo:
    """附件信息"""
    filename: str
    content: bytes
    content_type: str


def decode_mime_header(header_value) -> str:
    """解码 MIME 编码的邮件头"""
    if header_value is None:
        return ""
    parts = decode_header(header_value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                result.append(part.decode(charset or "utf-8", errors="replace"))
            except Exception:
                result.append(part.decode("utf-8", errors="replace"))
        else:
            result.append(str(part))
    return "".join(result)


def extract_body(msg) -> str:
    """提取邮件正文（优先text/plain，回退text/html）"""
    body_parts = []

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))

            if "attachment" in content_disposition:
                continue

            if content_type == "text/plain":
                charset = part.get_content_charset() or "utf-8"
                try:
                    body_parts.append(("plain", part.get_payload(decode=True).decode(charset, errors="replace")))
                except Exception:
                    pass
            elif content_type == "text/html":
                charset = part.get_content_charset() or "utf-8"
                try:
                    body_parts.append(("html", part.get_payload(decode=True).decode(charset, errors="replace")))
                except Exception:
                    pass
    else:
        content_type = msg.get_content_type()
        charset = msg.get_content_charset() or "utf-8"
        try:
            body_parts.append(("plain", msg.get_payload(decode=True).decode(charset, errors="replace")))
        except Exception:
            pass

    # 优先纯文本
    for tp, text in body_parts:
        if tp == "plain" and text.strip():
            return text.strip()

    # 回退 HTML → 纯文本
    for tp, text in body_parts:
        if tp == "html" and text.strip():
            from bs4 import BeautifulSoup
            return BeautifulSoup(text, "html.parser").get_text(separator="\n", strip=True)

    return ""


def extract_attachments(msg) -> list[AttachmentInfo]:
    """提取邮件附件（支持 Content-Disposition: attachment 和 inline 两种方式）"""
    attachments = []
    if not msg.is_multipart():
        return attachments

    for part in msg.walk():
        content_disposition = str(part.get("Content-Disposition", ""))
        filename = part.get_filename()
        content_type = part.get_content_type()

        # 跳过邮件正文（纯文本/HTML）
        if content_type in ("text/plain", "text/html") and not filename:
            continue

        # 包含以下任一条件即视为附件：
        # 1. Content-Disposition 包含 "attachment"
        # 2. 有文件名（即使 disposition 是 inline）
        # 3. 非文本/非多部分且 Content-Disposition 包含 "inline"
        if "attachment" not in content_disposition and not filename:
            continue

        if filename:
            filename = decode_mime_header(filename)

        content = part.get_payload(decode=True)
        if content is None:
            continue

        attachments.append(AttachmentInfo(
            filename=filename or "unnamed_attachment",
            content=content,
            content_type=content_type,
        ))

    return attachments


def _parse_email(raw_bytes: bytes) -> Optional[ParsedEmail]:
    """解析原始邮件字节"""
    try:
        msg = email.message_from_bytes(raw_bytes)
    except Exception as e:
        logger.error(f"解析邮件失败: {e}")
        return None

    message_id = msg.get("Message-ID", "")
    # 缺少 Message-ID 时用邮件内容 hash 生成合成 ID，避免 UNIQUE 约束冲突
    if not message_id:
        message_id = "synth-" + hashlib.sha256(raw_bytes).hexdigest()
    subject = decode_mime_header(msg["Subject"])
    sender = decode_mime_header(msg["From"])
    # 收件人：To + Cc；若均为空（密送 Bcc 收到），回退到投递地址头
    recipient_parts = [
        p for p in (
            decode_mime_header(msg["To"]),
            decode_mime_header(msg["Cc"]),
        ) if p
    ]
    if not recipient_parts:
        delivered = (
            decode_mime_header(msg["Delivered-To"])
            or decode_mime_header(msg["X-Original-To"])
        )
        if delivered:
            recipient_parts.append(delivered)
    recipient = ", ".join(recipient_parts)
    date_str = msg["Date"]

    try:
        date = parsedate_to_datetime(date_str)
    except Exception:
        date = datetime.now()

    body = extract_body(msg)
    attachments = extract_attachments(msg)

    # 提取关键邮件头用于转发副本检测等问题排查
    headers = {}
    for hdr in ["X-Forwarded-By", "X-Forwarded-For", "List-Id", "Precedence"]:
        val = msg.get(hdr, "")
        if val:
            headers[hdr.lower()] = val

    return ParsedEmail(
        message_id=message_id,
        subject=subject,
        sender=sender,
        date=date,
        body_text=body,
        recipient=recipient,
        attachments=attachments,
        headers=headers,
    )


# ============================================================
# 163.com 专用：raw SSL socket + ID command
# ============================================================

class Mail163Fetcher:
    """163.com 邮件拉取（raw socket + 缓冲 I/O）"""

    def __init__(self, host: str, port: int, username: str, password: str,
                 socket_timeout: int = 30, read_timeout: int = 30):
        self.host = host
        self.port = port
        self.username = username
        # 避免 __repr__ 或 traceback 泄露密码明文
        self._password = password
        self._sock: Optional[ssl.SSLSocket] = None
        self._buf = None  # 缓冲读取器
        self._tag_counter = 0
        self._socket_timeout = socket_timeout
        self._read_timeout = read_timeout

    def __repr__(self) -> str:
        return f"<Mail163Fetcher {self.username}@{self.host}>"

    def _tag(self) -> str:
        self._tag_counter += 1
        return f"A{self._tag_counter:04d}"

    def _connect(self):
        ctx = ssl.create_default_context()
        sock = socket.create_connection(
            (self.host, self.port), timeout=self._socket_timeout
        )
        self._sock = ctx.wrap_socket(sock, server_hostname=self.host)
        self._sock.settimeout(self._read_timeout)  # 防止 recv 永久阻塞
        self._buf = self._sock.makefile("rb", buffering=16384)  # 16KB 缓冲

    def _read_line(self) -> Optional[str]:
        try:
            line = self._buf.readline()
            if not line:
                return None
            return line.decode("utf-8", errors="replace").rstrip("\r\n")
        except (ssl.SSLError, socket.timeout, OSError):
            return None

    def _cmd(self, cmd: bytes, tag: str) -> list[str]:
        self._sock.sendall(cmd + b"\r\n")
        lines = []
        while True:
            line = self._read_line()
            if line is None:
                break
            lines.append(line)
            if line.startswith(tag + " "):
                break
        return lines

    def _read_literal(self, size: int) -> bytes:
        # 防御：拒绝超大 literal（超过 50MB 拒绝，防止 OOM）
        MAX_LITERAL = 50 * 1024 * 1024
        if size > MAX_LITERAL:
            raise ConnectionError(
                f"IMAP literal 过大 ({size} bytes)，超过安全上限 {MAX_LITERAL}"
            )
        data = self._buf.read(size)
        if len(data) != size:
            raise ConnectionError(
                f"IMAP 数据读取不完整：期望 {size} 字节，实际收到 {len(data)} 字节"
            )
        self._read_line()  # trailing CRLF
        return data

    def login(self):
        self._connect()
        self._read_line()  # greeting
        self._cmd(b"A0001 CAPABILITY", "A0001")
        # ID command — required for 163.com
        self._cmd(b'A0002 ID ("name" "Thunderbird" "version" "128.0")', "A0002")
        lines = self._cmd(
            f'A0003 LOGIN {_imap_astring(self.username)} {_imap_astring(self._password)}'.encode(),
            "A0003"
        )
        if not any("A0003 OK" in line for line in lines):
            raise Exception(f"163.com登录失败: {lines}")

    def select_inbox(self):
        lines = self._cmd(b'A0004 SELECT "INBOX"', "A0004")
        if not any("A0004 OK" in line for line in lines):
            raise Exception(f"SELECT INBOX 失败: {lines}")

    # 注意: search_unseen 已移除（改用 search_recent + 业务层去重）

    def search_recent(self, days: int = 7) -> list[str]:
        """搜索最近N天的全部邮件（含已读）"""
        since_date = datetime.now() - timedelta(days=days)
        since_str = f"{since_date.day:02d}-{_IMAP_MONTHS[since_date.month - 1]}-{since_date.year}"
        lines = self._cmd(f'A0006 SEARCH SINCE {since_str}'.encode(), "A0006")
        ids = []
        for line in lines:
            m = re.search(r"\* SEARCH (.+)", line)
            if m and m.group(1).strip():
                ids = m.group(1).split()
        return sorted(ids, key=int)

    def fetch_email(self, msg_id: str) -> Optional[bytes]:
        """拉取单封邮件原始内容"""
        tag = f"F{msg_id}"
        cmd = f'{tag} FETCH {msg_id} BODY.PEEK[]'.encode()
        self._sock.sendall(cmd + b"\r\n")

        line = self._read_line()
        if line is None:
            return None

        lit_match = re.search(r"\{(\d+)\}", line)
        if lit_match:
            size = int(lit_match.group(1))
            raw_data = self._read_literal(size)
            self._read_line()  # trailing tagged response
            return raw_data

        # No literal, read until tagged response
        while True:
            resp_line = self._read_line()
            if resp_line is None or resp_line.startswith(tag + " "):
                break
        return None

    def mark_seen(self, msg_id: str):
        """标记为已读"""
        # IMAP 命令格式: <tag> STORE <msg_id> +FLAGS (\Seen)
        # tag 必须是首个无空格 token（原实现 "A MARK{id}..." 导致命令字非法、永远等不到响应）
        self._cmd(f"AMARK{msg_id} STORE {msg_id} +FLAGS (\\Seen)".encode(), f"AMARK{msg_id}")

    def logout(self):
        if self._sock:
            try:
                self._cmd(b"A9999 LOGOUT", "A9999")
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass


# ============================================================
# 标准 IMAP 拉取（imaplib）
# ============================================================

class StandardFetcher:
    """标准IMAP邮件拉取"""

    def __init__(self, host: str, port: int, username: str, password: str, use_ssl: bool = True):
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self.use_ssl = use_ssl
        self._mail = None

    def __repr__(self) -> str:
        return f"<StandardFetcher {self.username}@{self.host}>"


    def _get_imap(self):
        import imaplib
        if self.use_ssl:
            return imaplib.IMAP4_SSL(self.host, self.port)
        return imaplib.IMAP4(self.host, self.port)

    def login(self):
        self._mail = self._get_imap()
        self._mail.login(self.username, self._password)

    def select_inbox(self):
        status, data = self._mail.select("INBOX")
        if status != "OK":
            raise Exception(f"SELECT INBOX 失败: {data}")

    def search_recent(self, days: int = 7) -> list[str]:
        """搜索最近N天全部邮件（含已读）"""
        since_date = datetime.now() - timedelta(days=days)
        since_str = f"{since_date.day:02d}-{_IMAP_MONTHS[since_date.month - 1]}-{since_date.year}"
        status, data = self._mail.search(None, f'SINCE "{since_str}"')
        if status != "OK" or not data or not data[0]:
            logger.warning(
                f"IMAP SEARCH 返回空结果(status={status})，可能原因："
                f"日期格式不被服务器支持（使用的月份: {_IMAP_MONTHS[since_date.month - 1]}）。"
                f"将尝试回退到 FETCH 全部邮件后按日期过滤..."
            )
            # 回退：拉取全部邮件后按日期在业务层过滤
            try:
                status, data = self._mail.search(None, "ALL")
                if status == "OK" and data and data[0]:
                    all_ids = data[0].split()
                    # 只保留最近的 N 天邮件（在 FETCH 时按日期过滤）
                    logger.info(f"回退搜索到 {len(all_ids)} 封邮件，将在提取时过滤日期")
                    return all_ids[-200:]  # 最多取最近200封防止内存溢出
            except Exception as fallback_err:
                logger.error(f"回退搜索也失败: {fallback_err}")
            return []
        return data[0].split()

    def fetch_email(self, msg_id: str) -> Optional[bytes]:
        """拉取单封邮件"""
        status, data = self._mail.fetch(msg_id, "(RFC822)")
        if status != "OK":
            return None
        # data[0] 可能是 (header, body_bytes) 或直接是 bytes
        raw_item = data[0]
        if isinstance(raw_item, (tuple, list)) and len(raw_item) > 1:
            raw = raw_item[1]
        elif isinstance(raw_item, bytes):
            raw = raw_item
        else:
            return None

        if isinstance(raw, bytes):
            return raw
        return None

    def mark_seen(self, msg_id: str):
        self._mail.store(msg_id, "+FLAGS", "\\Seen")

    def logout(self):
        if self._mail:
            try:
                self._mail.logout()
            except Exception:
                pass


# ============================================================
# 统一接口
# ============================================================

def fetch_new_emails(
    imap_host: str,
    imap_port: int,
    username: str,
    password_encrypted: str,
    provider_type: str = "auto",
    use_ssl: bool = True,
    days: int = 7,
    filter_sender: str = "",
    download_attachments: bool = True,
    global_blacklist: str = "",
) -> list[ParsedEmail]:
    """
    拉取最近N天的全部邮件（不依赖IMAP已读/未读标记）
    
    去重逻辑在上层 check_account() 中通过 EmailLog.message_id 实现。
    这里只负责从 IMAP 拉取原始邮件列表，不做任何标记操作。
    
    发件人过滤顺序：先合并全局黑名单 + 账户级黑名单，再统一过滤。
    任一黑名单命中即跳过。
    """
    password = decrypt(password_encrypted)

    # Determine fetcher type
    is_163 = provider_type == "163" or imap_host.endswith("163.com") or imap_host == "imap.163.com"

    if is_163:
        logger.info(f"使用 163.com raw socket 模式: {imap_host}")
        fetcher = Mail163Fetcher(imap_host, imap_port, username, password)
    else:
        logger.info(f"使用标准 IMAP 模式: {imap_host}")
        fetcher = StandardFetcher(imap_host, imap_port, username, password, use_ssl)

    try:
        fetcher.login()
        fetcher.select_inbox()

        # 统一扫描最近N天的全部邮件（不区分已读/未读）
        msg_ids = fetcher.search_recent(days=days)
        logger.info(f"发现 {len(msg_ids)} 封最近 {days} 天邮件")

        # ── 合并全局黑名单 + 账户级发件人过滤 ──
        account_senders = [s.strip().lower() for s in filter_sender.split(",") if s.strip()]
        global_senders = [s.strip().lower() for s in global_blacklist.split(",") if s.strip()]
        # 合并去重（保持顺序不变不影响逻辑）
        all_senders = account_senders + [s for s in global_senders if s not in account_senders]

        results = []
        filtered_count = 0
        for msg_id in msg_ids:
            try:
                raw = fetcher.fetch_email(msg_id)
                if raw is None:
                    continue

                parsed = _parse_email(raw)
                if parsed is None:
                    continue

                # 发件人过滤（黑名单：任一列表命中即跳过）
                if all_senders:
                    sender_lower = parsed.sender.lower()
                    if any(fs in sender_lower for fs in all_senders):
                        filtered_count += 1
                        logger.info(f"⛔ 跳过(发件人被过滤) [{filtered_count}]: {parsed.sender}")
                        continue

                # 不调用 mark_seen —— 去重完全依赖 DB 中的 Message-ID
                results.append(parsed)
                logger.info(f"已拉取: {parsed.subject}")

            except Exception as e:
                logger.error(f"拉取邮件 {msg_id} 失败: {e}")

        logger.info(f"发件人过滤完成: 保留 {len(results)} 封, 过滤 {filtered_count} 封")
        return results

    finally:
        fetcher.logout()

def _run_async_safe(coro):
    """安全地在同步代码中运行 async 函数，兼容已有 event loop 的场景

    当当前线程无 event loop 时使用 asyncio.run（APScheduler 独立线程场景）；
    当已有 event loop 时在新线程中运行（避免嵌套 loop 冲突）。
    """
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    else:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()


def _ocr_attachment(filename: str, content: bytes, ocr_cfg: dict | None = None) -> str:
    """对图片附件执行 OCR，返回识别文本"""
    from app.services.ocr import ocr_image

    if ocr_cfg is None:
        from app.database import SessionLocal
        from app.models import OCRConfig
        db = SessionLocal()
        try:
            ocr_row = db.query(OCRConfig).filter_by(is_active=True).first()
            if not ocr_row:
                logger.warning("无激活的 OCR 配置，跳过图片 OCR")
                return ""
            ocr_cfg = {
                "provider_type": ocr_row.provider_type,
                "api_url": ocr_row.api_url,
                "api_key": decrypt(ocr_row.api_key_encrypted) if ocr_row.api_key_encrypted else "",
                "model_name": ocr_row.model_name,
            }
        finally:
            db.close()

    try:
        text = _run_async_safe(ocr_image(content, ocr_cfg, filename))
        return text or ""
    except Exception as e:
        logger.error(f"OCR 识别 {filename} 失败: [{type(e).__name__}] {e}", exc_info=True)
        return ""


def extract_attachment_texts(parsed_email: ParsedEmail, max_chars: int = 6000, ocr_cfg: dict | None = None) -> tuple[str, list[dict]]:
    """
    从附件中提取文本内容，供 LLM 分析和关键词匹配使用
    
    支持格式: .txt, .docx, .doc, .wps, .pdf, .xlsx/.xls, .jpg/.png 等图片(OCR)
    
    返回: (拼接后的文本, 未被OCR处理的图片列表)
          图片列表每项: {"filename": str, "content": bytes, "mime_type": str}
    """
    from io import BytesIO
    import docx
    
    def _extract_doc_with_antiword(data: bytes) -> str:
        """通过 antiword 外部命令从 .doc 文件中提取文本"""
        import subprocess
        import tempfile
        import os
        try:
            with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as tf:
                tf.write(data)
                tmp_path = tf.name
            try:
                result = subprocess.run(
                    ["antiword", "-w", "0", tmp_path],
                    capture_output=True, text=True, timeout=10
                )
                if result.returncode == 0 and result.stdout.strip():
                    logger.info(f"antiword 成功提取 .doc 文本 ({len(result.stdout)} 字符)")
                    return result.stdout.strip()
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except FileNotFoundError:
            logger.debug("antiword 未安装，跳过 .doc 外部提取")
        except Exception as e:
            logger.debug(f"antiword 提取 .doc 失败: {e}")
        return ""

    def _extract_doc_text(data: bytes) -> str:
        """从 .doc (OLE2) 文件中提取文本"""
        try:
            import olefile
            ole = olefile.OleFileIO(data)
            # WordDocument 流包含文本
            if ole.exists("WordDocument"):
                word_stream = ole.openstream("WordDocument").read()
                # 尝试提取 1Table 或 0Table 中的文本
                # 简化方法：直接从流中提取 UTF-16LE 编码的文本片段
                text = word_stream.decode("utf-16-le", errors="replace")
                # 过滤控制字符，保留可读内容
                readable = re.sub(r'[^\u4e00-\u9fff\u3000-\u303f\uff00-\uffefa-zA-Z0-9\s.,;:!?()（）《》【】\-+=/%@#&*"\']+', '\n', text)
                lines = [ln.strip() for ln in readable.split('\n') if len(ln.strip()) > 2]
                return '\n'.join(lines[:500])
            ole.close()
        except Exception:
            pass
        return ""

    def _extract_binary_fallback(data: bytes) -> str:
        """最后降级：从任意二进制中提取可读文本片段"""
        text = data.decode("utf-8", errors="replace")
        readable = re.findall(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffefa-zA-Z0-9\s.,;:!?()（）《》【】\-\+%=/\\@#$&*\"']{4,}", text)
        return "\n".join(readable[:300])
    
    texts = []
    unocr_images = []  # 未被 OCR 处理的图片（供多模态 LLM 降级使用）
    
    for att in parsed_email.attachments:
        filename = att.filename or "unknown"
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        content = att.content

        if not content:
            continue

        try:
            extracted = None
            if ext in ("txt", "md", "csv", "log"):
                extracted = content.decode("utf-8", errors="replace")
            elif ext == "docx":
                doc = docx.Document(BytesIO(content))
                extracted = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
            elif ext == "doc":
                # 1) 尝试 python-docx（可能是伪装的 docx）
                # 2) antiword 外部调用（真正的老 .doc 格式）
                # 3) olefile 解析 OLE2 格式
                # 4) 二进制降级提取
                try:
                    doc = docx.Document(BytesIO(content))
                    extracted = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
                except Exception:
                    extracted = _extract_doc_with_antiword(content) or _extract_doc_text(content) or _extract_binary_fallback(content)
            elif ext == "wps":
                # WPS 也是 OLE2 格式，用 doc 提取器 + 二进制降级
                extracted = _extract_doc_text(content) or _extract_binary_fallback(content)
            elif ext == "pdf":
                try:
                    import fitz
                    doc = fitz.open(stream=content, filetype="pdf")
                    pages_text = []
                    pdf_images = []  # 扫描页图像（供 OCR / 多模态降级）

                    for page in doc:
                        text = page.get_text()
                        if text.strip():
                            pages_text.append(text)
                        else:
                            # 文字层为空 → 该页可能是扫描件，渲染为图像
                            try:
                                pix = page.get_pixmap(dpi=200)
                                img_bytes = pix.tobytes("png")
                                pdf_images.append({
                                    "filename": f"{filename}_p{page.number + 1}.png",
                                    "content": img_bytes,
                                    "mime_type": "image/png",
                                })
                            except Exception:
                                pass  # 渲染失败，跳过该页

                    doc.close()
                    extracted = "\n".join(pages_text) if pages_text else ""

                    # 扫描件 PDF：对提取的图像做 OCR
                    if pdf_images and not extracted:
                        ocr_texts = []
                        for img in pdf_images:
                            ocr_result = _ocr_attachment(
                                img["filename"], img["content"], ocr_cfg=ocr_cfg
                            )
                            if ocr_result:
                                ocr_texts.append(ocr_result)
                        if ocr_texts:
                            extracted = "\n".join(ocr_texts)
                            logger.info(
                                f"PDF 扫描件 OCR 完成: {filename} "
                                f"({len(pdf_images)} 页, {len(extracted)} 字符)"
                            )

                    # OCR 也失败 → 保留图像供多模态 LLM 降级
                    if not extracted and pdf_images:
                        mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                                    "png": "image/png", "bmp": "image/bmp",
                                    "tiff": "image/tiff", "webp": "image/webp"}
                        for img in pdf_images[:5]:  # 最多 5 页
                            ext_img = img["filename"].rsplit(".", 1)[-1].lower() if "." in img["filename"] else "png"
                            unocr_images.append({
                                "filename": img["filename"],
                                "content": img["content"],
                                "mime_type": mime_map.get(ext_img, "image/png"),
                            })
                        logger.info(
                            f"PDF 扫描件 {filename}: 文字层+OCR 均无结果，"
                            f"{len(pdf_images[:5])} 页图像将尝试多模态降级"
                        )

                except ImportError:
                    logger.warning("pymupdf not installed, skipping PDF text extraction")
                except Exception as e:
                    logger.warning(f"PDF extraction failed for {filename}: [{type(e).__name__}] {e}")
            elif ext in ("xlsx", "xls"):
                try:
                    import openpyxl
                    wb = openpyxl.load_workbook(BytesIO(content), data_only=True)
                    rows = []
                    for sheet_name in wb.sheetnames[:3]:
                        ws = wb[sheet_name]
                        rows.append(f"[Sheet: {sheet_name}]")
                        for row in ws.iter_rows(values_only=True, max_row=50):
                            rows.append(" | ".join(str(c) if c is not None else "" for c in row))
                    extracted = "\n".join(rows[:200])
                    wb.close()
                except ImportError:
                    logger.warning("openpyxl not installed, skipping Excel extraction")
            elif ext in ("jpg", "jpeg", "png", "bmp", "tiff", "webp"):
                # OCR 识别图片附件
                extracted = _ocr_attachment(filename, content, ocr_cfg=ocr_cfg)
                if not extracted:
                    # OCR 不可用/失败 → 保留图片供多模态 LLM 降级
                    mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                                "bmp": "image/bmp", "tiff": "image/tiff", "webp": "image/webp"}
                    unocr_images.append({
                        "filename": filename,
                        "content": content,
                        "mime_type": mime_map.get(ext, "image/png"),
                    })
            else:
                logger.debug(f"跳过不支持的文件类型: {ext} ({filename})")
                continue

            if extracted and extracted.strip():
                label = f"=== 附件: {filename} ==="
                texts.append(f"{label}\n{extracted.strip()}")

        except Exception as e:
            logger.error(f"提取附件 {filename} 文本失败: {e}")

    if not texts:
        combined = ""
    else:
        combined = "\n\n".join(texts)
        if len(combined) > max_chars:
            combined = combined[:max_chars] + "\n... (附件内容已截断)"
    
    return combined, unocr_images


def extract_per_attachment_texts(attachments: list[AttachmentInfo], ocr_cfg: dict | None = None, max_chars: int = 6000) -> dict:
    """
    按附件分别提取文本内容，供预分类和分组分析使用。
    
    attachments: 待提取的附件列表（可以是整封邮件的子集）
    返回: {index: {"filename": str, "text": str, "images": list[dict]}}
          images 为未被 OCR 处理的图片（供多模态 LLM 降级用）
    """
    from io import BytesIO

    def _doc_antiword(data: bytes) -> str:
        import subprocess, tempfile
        try:
            with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as tf:
                tf.write(data)
                tmp_path = tf.name
            try:
                r = subprocess.run(["antiword", "-w", "0", tmp_path], capture_output=True, text=True, timeout=10)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout.strip()
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception:
            pass
        return ""

    def _doc_ole(data: bytes) -> str:
        try:
            import olefile
            ole = olefile.OleFileIO(data)
            if ole.exists("WordDocument"):
                text = ole.openstream("WordDocument").read().decode("utf-16-le", errors="replace")
                readable = re.sub(r'[^\u4e00-\u9fff\u3000-\u303f\uff00-\uffefa-zA-Z0-9\s.,;:!?()（）《》【】\-+=/%@#&*"\']+', '\n', text)
                lines = [ln.strip() for ln in readable.split('\n') if len(ln.strip()) > 2]
                ole.close()
                return '\n'.join(lines[:500])
            ole.close()
        except Exception:
            pass
        return ""

    def _binary_fallback(data: bytes) -> str:
        text = data.decode("utf-8", errors="replace")
        readable = re.findall(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffefa-zA-Z0-9\s.,;:!?()（）《》【】\-+=/\\@#$&*\"']{4,}", text)
        return "\n".join(readable[:300])

    def _ocr_image(filename: str, content: bytes, ocr_cfg: dict | None) -> str:
        try:
            from app.services.ocr import ocr_image
            result = _run_async_safe(ocr_image(content, ocr_cfg or {}, filename))
            return result or ""
        except Exception as e:
            logger.warning(f"OCR 失败: {filename}: [{type(e).__name__}] {e}")
            return ""

    results = {}
    for idx, att in enumerate(attachments):
        filename = att.filename or "unknown"
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        content = att.content
        if not content:
            results[idx] = {"filename": filename, "text": "", "images": []}
            continue

        extracted = None
        unocr_img = []
        mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "bmp": "image/bmp", "tiff": "image/tiff", "webp": "image/webp"}

        try:
            if ext in ("txt", "md", "csv", "log"):
                extracted = content.decode("utf-8", errors="replace")
            elif ext == "docx":
                import docx
                doc = docx.Document(BytesIO(content))
                extracted = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
            elif ext == "doc":
                try:
                    import docx
                    doc = docx.Document(BytesIO(content))
                    extracted = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
                except Exception:
                    extracted = _doc_antiword(content) or _doc_ole(content) or _binary_fallback(content)
            elif ext == "wps":
                extracted = _doc_ole(content) or _binary_fallback(content)
            elif ext == "pdf":
                # ── PDF 直读路径（服务支持 PDF 时优先） ──
                if ocr_cfg and ocr_cfg.get("pdf_capable") is True:
                    from app.services.ocr import ocr_pdf
                    try:
                        pdf_text = _run_async_safe(ocr_pdf(content, ocr_cfg, filename))
                        if pdf_text and pdf_text.strip():
                            extracted = pdf_text.strip()
                            logger.info(f"PDF 直读成功: {filename} ({len(extracted)} 字符)")
                    except Exception as e:
                        logger.warning(f"PDF 直读失败 {filename}，将回退逐页渲染: {e}")
                        extracted = None

                # ── 兜底：逐页渲染 + OCR ──
                if not extracted:
                    import fitz
                    doc = fitz.open(stream=content, filetype="pdf")
                    pages_text = []
                    pdf_images = []
                    for page in doc:
                        text = page.get_text()
                        if text.strip():
                            pages_text.append(text)
                        else:
                            try:
                                pix = page.get_pixmap(dpi=200)
                                pdf_images.append(pix.tobytes("png"))
                            except Exception:
                                pass
                    doc.close()
                    extracted = "\n".join(pages_text) if pages_text else ""
                    if pdf_images and not extracted:
                        ocr_texts = []
                        for img_bytes in pdf_images:
                            ocr_result = _ocr_image(f"{filename}_p.png", img_bytes, ocr_cfg)
                            if ocr_result:
                                ocr_texts.append(ocr_result)
                        if ocr_texts:
                            extracted = "\n".join(ocr_texts)
                    if not extracted and pdf_images:
                        for img_bytes in pdf_images[:5]:
                            unocr_img.append({"filename": f"{filename}_p.png", "content": img_bytes, "mime_type": "image/png"})
            elif ext in ("xlsx", "xls"):
                import openpyxl
                wb = openpyxl.load_workbook(BytesIO(content), data_only=True)
                rows = []
                for sheet_name in wb.sheetnames[:3]:
                    ws = wb[sheet_name]
                    rows.append(f"[Sheet: {sheet_name}]")
                    for row in ws.iter_rows(values_only=True, max_row=50):
                        rows.append(" | ".join(str(c) if c is not None else "" for c in row))
                extracted = "\n".join(rows[:200])
                wb.close()
            elif ext in ("jpg", "jpeg", "png", "bmp", "tiff", "webp"):
                extracted = _ocr_image(filename, content, ocr_cfg)
                if not extracted:
                    unocr_img.append({"filename": filename, "content": content, "mime_type": mime_map.get(ext, "image/png")})
            else:
                logger.debug(f"跳过不支持的文件类型: {ext} ({filename})")
        except Exception as e:
            logger.error(f"提取附件 {filename} 文本失败: {e}")

        results[idx] = {
            "filename": filename,
            "text": (extracted or "").strip()[:max_chars] if extracted else "",
            "images": unocr_img,
        }

    return results


def save_attachments(parsed_email: ParsedEmail, log_id: int, account_name: str = "unknown") -> list[dict]:
    """保存附件到磁盘，按「账户名/日期」分目录，返回附件信息列表"""
    saved = []
    # 安全账户名：处理非法字符 + 边界情况
    safe_account = account_name.strip()
    # 替换 Windows 非法文件名字符
    safe_account = re.sub(r'[\\/:*?"<>|]', '_', safe_account)
    # 清理路径穿越组件（防目录逃逸到 data/ 之外）
    safe_account = safe_account.replace("..", "_")
    # 移除首尾空格和点号（Windows 不兼容）
    safe_account = safe_account.strip('. ')
    # 处理 Windows 保留名（CON, PRN, AUX, NUL, COM1-9, LPT1-9）
    _WIN_RESERVED = {
        'con', 'prn', 'aux', 'nul',
        *(f'com{i}' for i in range(1, 10)),
        *(f'lpt{i}' for i in range(1, 10)),
    }
    if safe_account.lower() in _WIN_RESERVED:
        safe_account = f'_{safe_account}'
    # 限制长度（路径组件上限 ~255）
    if len(safe_account) > 200:
        safe_account = safe_account[:200]
    # 空名兜底
    if not safe_account:
        safe_account = 'unknown'
    date_str = parsed_email.date.strftime("%Y-%m-%d") if parsed_email.date else "unknown"
    date_dir = ATTACHMENTS_DIR / safe_account / date_str
    date_dir.mkdir(parents=True, exist_ok=True)

    for att in parsed_email.attachments:
        # 安全文件名（清洗路径穿越组件，防目录逃逸）
        safe_name = re.sub(r'[\\/:*?"<>|]', '_', att.filename).replace("..", "_").strip('. ')
        file_path = date_dir / safe_name

        # 重名处理
        counter = 1
        while file_path.exists():
            if "." in safe_name:
                stem, ext = safe_name.rsplit(".", 1)
            else:
                stem, ext = safe_name, ""
            file_path = date_dir / f"{stem}_{counter}.{ext}" if ext else date_dir / f"{stem}_{counter}"
            counter += 1

        file_path.write_bytes(att.content)
        saved.append({
            "filename": att.filename,
            "file_path": str(file_path.relative_to(ATTACHMENTS_DIR.parent)),
            "file_size": len(att.content),
        })

    return saved
