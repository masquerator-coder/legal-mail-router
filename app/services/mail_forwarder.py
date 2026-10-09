"""
邮件转发模块 — SMTP 转发到目标律师
"""
import smtplib
import logging
import tempfile
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from pathlib import Path
from typing import Optional
from app.config import decrypt
from app.config import get_system_name  # noqa: F401  (SYSTEM_NAME 改用函数动态读取)
from app.services.email_fetcher import FORWARD_COPY_MARKER  # 转发副本标记（纯 ASCII，单一来源）

logger = logging.getLogger(__name__)


def _safe_pct(val) -> str:
    """安全的百分比格式化"""
    try:
        return f"{float(val or 0):.0%}"
    except (ValueError, TypeError):
        return "N/A"


def _sanitize_header(value: str) -> str:
    """邮件头字段禁止含 CR/LF（防邮件头注入）"""
    return value.replace("\r", " ").replace("\n", " ")


def _build_email_body(
    to_name: str,
    analyses: list[dict],
    original_subject: str,
    original_body: str = "",
    brief_mode: bool = False,
    original_sender: str = "",
    original_recipient: str = "",
    original_date: str | datetime = "",
) -> str:
    """构建邮件正文（支持多分析结果）

    analyses: LLM 分析结果列表，每组对应一份独立文书
    original_date: 原邮件接收日期（datetime 或已格式化字符串，可选）
    """
    urgency_map = {"high": "🔴 紧急", "medium": "🟡 一般", "low": "🟢 普通"}

    # 原邮件元信息（发件人/收件人/日期/主题），缺失字段自动省略
    origin_meta = []
    if original_sender:
        origin_meta.append(f"原发件人：{original_sender}")
    if original_recipient:
        origin_meta.append(f"原收件人：{original_recipient}")
    if original_date:
        if isinstance(original_date, datetime):
            if original_date.tzinfo is not None:
                # aware datetime：转换为本地时区，避免跨时区邮件显示原时区时间
                original_date = original_date.astimezone()
            original_date = original_date.strftime("%Y-%m-%d %H:%M")
        origin_meta.append(f"原收件日期：{original_date}")
    origin_meta.append(f"原邮件主题：{original_subject}")
    origin_block = "\n".join(origin_meta)

    # 原邮件正文（截断过长内容）
    body_display = original_body or "（无正文）"
    if len(body_display) > 3000:
        body_display = body_display[:3000] + "\n... (原文过长已截断，请登录监控邮箱查看完整内容)"

    is_multi = len(analyses) > 1

    def _render_one(analysis: dict, idx: int = 0) -> str:
        urgency_text = urgency_map.get(analysis.get("urgency"), "🟡 一般")
        ai_interp = analysis.get('ai_interpretation', '')
        header = f"\n── 第 {idx + 1} 组：{analysis.get('doc_type', '文书')} ──\n" if is_multi else ""
        if brief_mode:
            return f"""{header}📋 文书类型：{analysis.get('doc_type', '未知')}
⚡ 紧急程度：{urgency_text}
📝 案件摘要：{analysis.get('case_summary', '无')}
🏛️ 涉及方：{analysis.get('involved_parties', '无')}
📅 关键日期：{analysis.get('key_date', '无')}
📎 案号：{analysis.get('case_number', '无')}
📊 分析置信度：{_safe_pct(analysis.get('confidence'))}
"""
        else:
            interp_section = ai_interp if ai_interp else "（暂无AI解读，请人工审核）"
            return f"""{header}📋 文书类型：{analysis.get('doc_type', '未知')}
⚡ 紧急程度：{urgency_text}
📝 案件摘要：{analysis.get('case_summary', '无')}
🏛️ 涉及方：{analysis.get('involved_parties', '无')}
📅 关键日期：{analysis.get('key_date', '无')}
📎 案号：{analysis.get('case_number', '无')}
📊 分析置信度：{_safe_pct(analysis.get('confidence'))}

🤖 AI 初步审核解读：
{interp_section}
"""

    if brief_mode:
        sections = []
        for i, a in enumerate(analyses):
            sections.append(_render_one(a, i))
        brief_summary = "系统收到一封法律文书邮件，共分析 {} 份文书。\n".format(
            len(analyses) if is_multi else ""
        )
        return f"""您好 {to_name}，

{brief_summary}
{chr(10).join(sections)}
📎 AI 初步审核解读详见附件《AI分析报告.docx》—— 请下载查阅完整解读内容。
━━━━━━━━━━━━━━━━━━━━

{origin_block}

📧 原邮件正文：
{body_display}
━━━━━━━━━━━━━━━━━━━━

此为自动转发，如需查看完整原始邮件请登录监控邮箱。

---
|{get_system_name()}
"""

    # 完整版正文
    sections = []
    for i, a in enumerate(analyses):
        sections.append(_render_one(a, i))
    section_sep = "\n" + ("━" * 40) + "\n" if is_multi else ""
    body_content = section_sep.join(sections)

    return f"""您好 {to_name}，

系统收到一封法律文书邮件，{'共分析 {} 份文书。'.format(len(analyses)) if is_multi else ''}AI 分析结果如下：

{body_content}

━━━━━━━━━━━━━━━━━━━━

{origin_block}

📧 原邮件正文：
{body_display}
━━━━━━━━━━━━━━━━━━━━

此为自动转发，如需查看完整原始邮件请登录监控邮箱。

---
|{get_system_name()}
"""


def _strip_markdown(text: str) -> str:
    """
    彻底去除文本中的 Markdown 标记，确保 Word 文档排版干净。

    清理的标记包括：
    - ## 标题标记
    - **加粗** / *斜体* / ***斜体加粗***
    - `行内代码`
    - ```代码块```
    - > 引用
    - --- / *** 水平线
    """
    import re

    # 1. 代码块（```...``` 或 ~~~...~~~）
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"~~~[\s\S]*?~~~", "", text)

    # 2. 行内代码 `...`
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # 3. 加粗+斜体 ***...***
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)

    # 4. 加粗 **...** 或 __...__
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)

    # 5. 斜体 *...* 或 _..._（注意避开数字、中文间的下划线）
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"\1", text)

    # 6. 标题标记（行首的 ## 等）
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)

    # 7. 引用标记 >（行首）
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)

    # 8. 水平线 --- / *** / ___
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)

    # 9. 清理多余空行（多个连续空行合并为1个）
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _generate_analysis_docx(analyses: list[dict], original_subject: str) -> Optional[str]:
    """
    将 AI 分析结果生成为 .docx 文件（支持多份文书），返回文件路径。

    如果 python-docx 不可用，返回 None 并记录警告。
    """
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except ImportError:
        logger.warning("python-docx 未安装，无法生成 Word 附件，将回退为邮件正文模式")
        return None

    doc = Document()
    is_multi = len(analyses) > 1

    # ── 标题 ──
    title = doc.add_heading("AI 初步审核解读报告", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    urgency_map = {"high": "🔴 紧急", "medium": "🟡 一般", "low": "🟢 普通"}

    for idx, analysis in enumerate(analyses):
        if not analysis:
            continue

        if is_multi:
            doc.add_heading(f"第 {idx + 1} 组：{analysis.get('doc_type', '文书')}", level=1)

        # ── 基本信息表 ──
        doc.add_heading("基本信息", level=2 if is_multi else 1)
        table = doc.add_table(rows=8, cols=2, style="Light Grid Accent 1")
        table.autofit = True

        fields = [
            ("文书类型", analysis.get("doc_type", "未知")),
            ("紧急程度", urgency_map.get(analysis.get("urgency"), "🟡 一般")),
            ("案件摘要", analysis.get("case_summary", "无")),
            ("涉及方", analysis.get("involved_parties", "无")),
            ("关键日期", analysis.get("key_date", "无")),
            ("案号", analysis.get("case_number", "无")),
            ("分析置信度", _safe_pct(analysis.get("confidence"))),
            ("原邮件主题", original_subject),
        ]
        for i, (key, val) in enumerate(fields):
            row = table.rows[i]
            row.cells[0].text = key
            row.cells[1].text = str(val)
            for paragraph in row.cells[0].paragraphs:
                for run in paragraph.runs:
                    run.bold = True

        # ── AI 解读正文 ──
        doc.add_heading("AI 初步审核解读", level=2 if is_multi else 1)
        ai_interp = analysis.get("ai_interpretation", "")
        if ai_interp:
            ai_interp = _strip_markdown(ai_interp)
            for line in ai_interp.split("\n"):
                p = doc.add_paragraph(line.strip())
                p.paragraph_format.space_after = Pt(4)
                p.paragraph_format.line_spacing = 1.35
        else:
            doc.add_paragraph("（暂无AI解读，请人工审核）")

        # ── 结构化修订指令 ──
        ri_list = analysis.get("revision_instructions", [])
        if ri_list and isinstance(ri_list, list) and len(ri_list) > 0:
            doc.add_heading("结构化修订指令", level=2 if is_multi else 1)
            ri_table = doc.add_table(rows=1, cols=4, style="Light Grid Accent 1")
            ri_table.autofit = True
            # 表头
            for i, header in enumerate(["操作", "位置", "问题", "建议修改"]):
                ri_table.rows[0].cells[i].text = header
                for paragraph in ri_table.rows[0].cells[i].paragraphs:
                    for run in paragraph.runs:
                        run.bold = True
            # 数据行
            action_labels = {"modify": "修改", "add": "新增", "delete": "删除"}
            for instr in ri_list:
                row_cells = ri_table.add_row().cells
                action = instr.get("action", "")
                row_cells[0].text = action_labels.get(action, action)
                row_cells[1].text = instr.get("target_location", "")
                row_cells[2].text = instr.get("issue", "")
                row_cells[3].text = instr.get("suggested_revision", "")

        if is_multi and idx < len(analyses) - 1:
            doc.add_page_break()

    # ── 尾部信息 ──
    doc.add_paragraph("")
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = p.add_run(f"—— {get_system_name()} 自动生成 ——")
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(128, 128, 128)

    # 写入临时文件
    tmp = tempfile.NamedTemporaryFile(
        suffix=".docx", prefix="AI分析报告_", delete=False
    )
    doc.save(tmp.name)
    logger.info(f"AI 分析报告已生成: {tmp.name}")
    return tmp.name


_SMTP_RETRY_DELAY = 5  # SMTP 重试间隔（秒）


def forward_email(
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password_encrypted: str,
    from_email: str,
    to_email: str,
    to_name: str,
    original_subject: str,
    original_body: str = "",
    original_sender: str = "",
    original_recipient: str = "",
    original_date: str | datetime = "",
    analyses_results: list[dict] = None,
    analysis_result: dict = None,
    attachment_paths: list[str] = None,
    analysis_output_mode: str = "content",
    retry_count: int = 2,
    retry_delay: int = _SMTP_RETRY_DELAY,
) -> tuple:
    """
    转发邮件（含分析摘要 + AI解读 + 原邮件正文 + 附件）

    参数:
        smtp_host: SMTP 服务器
        smtp_port: SMTP 端口
        smtp_username: SMTP 账号
        smtp_password_encrypted: 加密的 SMTP 密码
        from_email: 发件人（监控邮箱）
        to_email: 收件人
        to_name: 收件人姓名
        original_subject: 原邮件主题
        original_body: 原邮件正文
        original_sender: 原邮件发件人（可选，用于在转发正文中注明）
        original_recipient: 原邮件收件人（可选，用于在转发正文中注明）
        original_date: 原邮件接收日期（可选，用于在转发正文中注明）
        analyses_results: LLM 分析结果列表 (推荐，支持多文书)
        analysis_result: 单条 LLM 分析结果 (兼容旧调用)
        attachment_paths: 附件路径列表
        analysis_output_mode: 输出模式
        retry_count: SMTP 发送失败重试次数（默认 2 次）
        retry_delay: 重试间隔秒数（默认 5 秒）

    返回: (成功, 错误信息)
    """
    smtp_password = decrypt(smtp_password_encrypted)

    # 兼容旧调用：无 analyses_results 时包装 analysis_result
    if analyses_results is None:
        analyses = [analysis_result] if analysis_result else [{}]
    else:
        analyses = analyses_results if analyses_results else [{}]

    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email

    # 构建主题：去重后的文书类型（Subject 来自外部邮件/LLM，需清洗 CR/LF 防头注入）
    doc_types = []
    any_failed = False
    seen_dt = set()
    clean_subject = _sanitize_header(original_subject or "")
    for a in analyses:
        if a:
            if a.get("llm_failed"):
                any_failed = True
            dt = _sanitize_header(a.get("doc_type", ""))
            if dt and dt not in seen_dt:
                doc_types.append(dt)
                seen_dt.add(dt)
    if any_failed:
        msg["Subject"] = f"【大模型分析失败】{clean_subject}"
    elif doc_types:
        doc_type_str = " / ".join(doc_types)
        msg["Subject"] = f"【{doc_type_str}】{clean_subject}"
    else:
        msg["Subject"] = f"【法律文书】{clean_subject}"
    msg["X-Forwarded-By"] = FORWARD_COPY_MARKER
    msg["X-Forwarded-For"] = _sanitize_header(from_email)

    # ── 构建正文 ──
    if analysis_output_mode == "attachment":
        docx_path = _generate_analysis_docx(analyses, original_subject)
        body = _build_email_body(
            to_name, analyses, original_subject, original_body, brief_mode=True,
            original_sender=original_sender, original_recipient=original_recipient,
            original_date=original_date,
        )
        msg.attach(MIMEText(body, "plain", "utf-8"))

        if docx_path:
            path = Path(docx_path)
            with open(path, "rb") as f:
                part = MIMEApplication(f.read(), Name="AI分析报告.docx")
                part["Content-Disposition"] = 'attachment; filename="AI分析报告.docx"'
                msg.attach(part)
            # 临时报告已读入内存，立即清理，避免长期运行堆积
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                logger.warning(f"清理临时 AI 分析报告失败: {docx_path} — {e}")
    else:
        body = _build_email_body(
            to_name, analyses, original_subject, original_body, brief_mode=False,
            original_sender=original_sender, original_recipient=original_recipient,
            original_date=original_date,
        )
        msg.attach(MIMEText(body, "plain", "utf-8"))

    # ── 原邮件附件（带路径遍历防御）──
    from app.config import ATTACHMENTS_DIR
    _allowed_base = ATTACHMENTS_DIR.parent.resolve()
    import tempfile
    _temp_dir = Path(tempfile.gettempdir()).resolve()

    if attachment_paths:
        for file_path in attachment_paths:
            path = Path(file_path)
            # 路径遍历防御：只允许访问 ATTACHMENTS_DIR 目录树或系统临时目录内的文件
            # 系统生成的修改版文书/审核意见/AI分析报告在 TEMP 目录下，需放行
            try:
                resolved = path.resolve()
                is_in_attachments = _allowed_base in resolved.parents or resolved == _allowed_base
                is_in_temp = _temp_dir in resolved.parents or resolved == _temp_dir
                if not (is_in_attachments or is_in_temp):
                    logger.warning(f"附件路径越界（已跳过）: {file_path}")
                    continue
            except (ValueError, OSError, RuntimeError):
                logger.warning(f"附件路径解析失败（已跳过）: {file_path}")
                continue

            if not resolved.exists():
                logger.warning(f"附件不存在: {file_path}")
                continue
            with open(resolved, "rb") as f:
                part = MIMEApplication(f.read(), Name=path.name)
                part["Content-Disposition"] = f'attachment; filename="{path.name}"'
                msg.attach(part)

    # 发送（带重试）
    import time

    last_error = ""
    server = None
    # 已知部分国内服务商(163/126 等)587 端口不支持 STARTTLS 会直接断开，
    # 遇到 SMTPServerDisconnected 时自动回退到 465 SSL 再试一次（不消耗重试次数）
    use_ssl_fallback = False
    attempt = 0
    while attempt < 1 + retry_count:
        if attempt > 0:
            logger.info(f"SMTP 重试 {attempt}/{retry_count}（等待 {retry_delay} 秒后）")
            time.sleep(retry_delay)

        server = None
        try:
            if smtp_port == 465 or use_ssl_fallback:
                import ssl as _ssl
                server = smtplib.SMTP_SSL(smtp_host, 465 if use_ssl_fallback else smtp_port,
                                          timeout=30, context=_ssl.create_default_context())
            else:
                server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
                server.starttls()

            server.login(smtp_username, smtp_password)
            server.sendmail(from_email, [to_email], msg.as_string())

            logger.info(f"邮件已转发: {original_subject} → {to_email}")
            return True, None

        except smtplib.SMTPAuthenticationError as e:
            last_error = f"认证失败：账号或授权码错误 (SMTP AUTH 报错: {e})"
            logger.error(f"转发邮件认证失败({attempt}/{retry_count}): {last_error}")
            # 认证失败不重试（配置问题，重试也没用）
            return False, last_error

        except smtplib.SMTPRecipientsRefused as e:
            last_error = f"收件人被拒绝：{to_email} (SMTP 报错: {e})"
            logger.error(f"转发邮件收件人被拒绝({attempt}/{retry_count}): {last_error}")
            return False, last_error

        except smtplib.SMTPSenderRefused as e:
            last_error = f"发件人被拒绝：{from_email} (SMTP 报错: {e})"
            logger.error(f"转发邮件发件人被拒绝({attempt}/{retry_count}): {last_error}")
            return False, last_error

        except smtplib.SMTPDataError as e:
            last_error = f"邮件数据被拒绝：服务器拒收邮件内容 (SMTP 报错: {e})"
            logger.error(f"转发邮件数据错误({attempt}/{retry_count}): {last_error}")
            return False, last_error

        except smtplib.SMTPConnectError as e:
            last_error = f"连接失败：无法连接到 {smtp_host}:{smtp_port} (SMTP 报错: {e})"
            logger.error(f"转发邮件连接失败({attempt}/{retry_count}): {last_error}")

        except smtplib.SMTPServerDisconnected as e:
            if smtp_port != 465 and not use_ssl_fallback:
                # 587(STARTTLS) 被服务器断开 → 回退 465 SSL 立即重试
                use_ssl_fallback = True
                logger.warning(
                    f"{smtp_host}:{smtp_port} STARTTLS 被服务器断开({e})，回退 465 SSL 重试"
                )
                continue  # 不消耗重试次数，不等待
            last_error = f"服务器断开：{smtp_host} 在转发过程中断开连接 (SMTP 报错: {e})"
            logger.error(f"转发邮件服务器断开({attempt}/{retry_count}): {last_error}")

        except smtplib.SMTPException as e:
            last_error = f"SMTP 错误：{smtp_host}:{smtp_port} (类型: {type(e).__name__}, 详情: {e})"
            logger.error(f"转发邮件 SMTP 异常({attempt}/{retry_count}): {last_error}")

        except TimeoutError:
            last_error = f"超时：连接 {smtp_host}:{smtp_port} 超过 30 秒无响应"
            logger.error(f"转发邮件超时({attempt}/{retry_count}): {last_error}")

        except ConnectionError as e:
            last_error = f"网络错误：无法连接到 {smtp_host}:{smtp_port} (详情: {e})"
            logger.error(f"转发邮件网络错误({attempt}/{retry_count}): {last_error}")

        except Exception as e:
            last_error = f"未知错误：转发到 {to_email} 失败 (类型: {type(e).__name__}, 详情: {e})"
            logger.error(f"转发邮件失败({attempt}/{retry_count}): {last_error}")

        finally:
            if server:
                try:
                    server.quit()
                except Exception:
                    pass

        attempt += 1

    # 所有重试都失败
    return False, last_error


def get_default_smtp_config(db_session) -> Optional[dict]:
    """获取默认 SMTP 配置"""
    from app.models import DefaultConfig

    smtp_host = db_session.query(DefaultConfig).filter_by(key="default_smtp_host").first()
    smtp_port = db_session.query(DefaultConfig).filter_by(key="default_smtp_port").first()
    smtp_user = db_session.query(DefaultConfig).filter_by(key="default_smtp_username").first()
    smtp_pass = db_session.query(DefaultConfig).filter_by(key="default_smtp_password").first()

    if smtp_host and smtp_user and smtp_pass:
        return {
            "host": smtp_host.value,
            "port": int(smtp_port.value) if smtp_port else 587,
            "username": smtp_user.value,
            "password_encrypted": smtp_pass.value,
        }
    return None


def dedupe_smtp_cfgs(smtp_cfgs: list) -> list:
    """SMTP 候选列表去重并过滤空值（按 host/port/username/密码 完全一致判定）"""
    seen = set()
    result = []
    for cfg in smtp_cfgs:
        if not cfg:
            continue
        key = (
            cfg.get("host"),
            cfg.get("port"),
            cfg.get("username"),
            cfg.get("password_encrypted"),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(cfg)
    return result


# ── 修改版文书 docx 生成 ──

def _parse_revision_markers(text: str) -> list[tuple[str, str]]:
    """将含修订标记的文本解析为 (tag, text) 序列。

    tag: "normal" | "add" | "modify" | "delete"

    使用栈式解析，对 LLM 输出的不规范标记做容错：
    - 开标记未闭合：着色延续到下一个标记或文本末尾（隐式闭合）
    - 同类型嵌套（如行首前缀式「【新增】…【新增】…」）：视作同一着色延续
    - 闭合标记与当前类型不匹配：忽略该闭合，维持当前着色
    - 标记文字本身不进入输出段（仅渲染颜色）
    """
    import re
    tag_map = {"新增": "add", "修改": "modify", "删除": "delete"}
    marker_re = re.compile(r"【(/?)(新增|修改|删除)】")

    segments: list[tuple[str, str]] = []
    stack: list[str] = []  # 未闭合的开标记类型栈
    pos = 0

    for m in marker_re.finditer(text):
        is_close = m.group(1) == "/"
        tag = tag_map[m.group(2)]
        if m.start() > pos:
            cur = stack[-1] if stack else "normal"
            segments.append((cur, text[pos:m.start()]))
        if is_close:
            if stack and stack[-1] == tag:  # 仅弹出匹配的栈顶，不匹配则忽略
                stack.pop()
        else:
            stack.append(tag)
        pos = m.end()

    if pos < len(text):
        cur = stack[-1] if stack else "normal"
        segments.append((cur, text[pos:]))

    # 合并相邻同类型段，过滤空段
    merged: list[tuple[str, str]] = []
    for tag, seg in segments:
        if not seg.strip():
            continue
        if merged and merged[-1][0] == tag:
            merged[-1] = (tag, merged[-1][1] + seg)
        else:
            merged.append((tag, seg))
    return merged if merged else [("normal", text)]


def _split_citation_section(text: str) -> tuple[str, str]:
    """把修订文书文本按「引用依据」小节切分为 (正文, 引用依据小节)。

    引用小节从首个以「引用依据」开头的行开始（兼容「引用依据：」「【引用依据】」
    「〔引用依据〕」等写法），其后的所有行都属于引用小节。这样可在 .docx 中把
    引用链接与正文在格式上区分开（隔两行 + 绿色字体）。
    未找到引用小节则返回 (原文, "")。
    """
    lines = text.split("\n")
    start = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith(("引用依据", "【引用依据】", "〔引用依据〕", "「引用依据」")):
            start = i
            break
    if start is None:
        return text, ""
    return "\n".join(lines[:start]), "\n".join(lines[start:])


def _generate_revision_docx(revision_text: str, doc_type: str,
                            original_subject: str, use_highlight: bool = True) -> Optional[str]:
    """
    将 LLM 生成的修改版文书渲染为带颜色标注的 .docx 文件。

    色彩规则：
    - 蓝色 = 新增内容
    - 红色 = 修改内容
    - 红色 + 删除线 = 建议删除

    返回临时文件路径，失败返回 None。
    """
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except ImportError:
        logger.warning("python-docx 未安装，无法生成修改版文书 docx")
        return None

    from datetime import datetime

    doc = Document()

    # ── 标题 ──
    title = doc.add_heading("修改版文书", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # ── 基本信息表 ──
    doc.add_heading("基本信息", level=1)
    table = doc.add_table(rows=3, cols=2, style="Light Grid Accent 1")
    fields = [
        ("文书类型", doc_type),
        ("原邮件主题", original_subject),
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M")),
    ]
    for i, (key, val) in enumerate(fields):
        row = table.rows[i]
        row.cells[0].text = key
        row.cells[1].text = str(val)
        for paragraph in row.cells[0].paragraphs:
            for run in paragraph.runs:
                run.bold = True

    # ── 修订正文 ──
    doc.add_heading("修订全文", level=1)

    # 把「引用依据」小节从正文中切出，便于下方单独渲染（隔两行 + 绿色）
    body_text, citation_text = _split_citation_section(revision_text)

    if use_highlight:
        segments = _parse_revision_markers(body_text)
        for tag, text in segments:
            if not text.strip():
                continue
            # 按段落拆分：标记内的文本可能含多个逻辑段落
            for line in text.split("\n"):
                line = line.strip()
                if not line:
                    continue
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(4)
                p.paragraph_format.line_spacing = 1.35
                run = p.add_run(line)

                if tag == "add":
                    run.font.color.rgb = RGBColor(0, 70, 200)       # 蓝色
                elif tag == "modify":
                    run.font.color.rgb = RGBColor(200, 0, 0)        # 红色
                elif tag == "delete":
                    run.font.color.rgb = RGBColor(180, 0, 0)        # 深红
                    run.font.strike = True                           # 删除线
                # normal 使用默认黑色
    else:
        # 无色彩模式：直接输出正文全文（含原始标记，由用户自行阅读）
        for line in body_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            p = doc.add_paragraph(line.strip())
            p.paragraph_format.space_after = Pt(4)
            p.paragraph_format.line_spacing = 1.35

    # ── 引用依据小节：与正文隔开两行，用绿色字体显示 ──
    if citation_text:
        doc.add_paragraph("")
        doc.add_paragraph("")
        for line in citation_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(4)
            p.paragraph_format.line_spacing = 1.35
            run = p.add_run(line)
            run.font.color.rgb = RGBColor(0, 128, 0)               # 绿色

    # ── 图例 ──
    if use_highlight:
        doc.add_paragraph("")
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        legend = p.add_run("图例：")
        legend.font.size = Pt(9)
        legend.font.color.rgb = RGBColor(100, 100, 100)

        for label, color in [("蓝色=新增", RGBColor(0, 70, 200)),
                              ("红色=修改", RGBColor(200, 0, 0)),
                              ("红色+删除线=建议删除", RGBColor(180, 0, 0))]:
            p2 = doc.add_paragraph()
            run_label = p2.add_run(f"  ■ {label}")
            run_label.font.size = Pt(9)
            run_label.font.color.rgb = color
            if "删除" in label:
                run_label.font.strike = True

    # ── 尾部 ──
    doc.add_paragraph("")
    footer = doc.add_paragraph()
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run_footer = footer.add_run(f"—— {get_system_name()} 自动生成 ——")
    run_footer.font.size = Pt(9)
    run_footer.font.color.rgb = RGBColor(128, 128, 128)

    # 写入临时文件
    tmp = tempfile.NamedTemporaryFile(
        suffix=".docx", prefix="修改版文书_", delete=False
    )
    doc.save(tmp.name)
    logger.info(f"修改版文书已生成: {tmp.name} ({len(revision_text)} 字符)")
    return tmp.name


def _extract_doc_title(filenames: list[str] | None) -> str | None:
    """从附件文件名中提取文书标题，如 'XX项目-咨询合同.doc' → 'XX项目-咨询合同'"""
    if not filenames:
        return None
    for fn in filenames:
        if not fn:
            continue
        # 去掉扩展名
        name = fn.rsplit(".", 1)[0] if "." in fn else fn
        # 跳过太短或无意义的名字
        if len(name) < 4:
            continue
        # 跳过常见非文书名
        skip_words = ["unnamed", "attachment", "附件", "image", "未命名"]
        if any(kw in name.lower() for kw in skip_words):
            continue
        return name
    # 所有文件名都不合适，返回第一个去掉扩展名的
    for fn in filenames:
        if fn:
            return fn.rsplit(".", 1)[0] if "." in fn else fn
    return None


def _fill_review_template(template_path: str, analysis: dict,
                          original_subject: str, sender: str = "",
                          body_text: str = "",
                          attachment_filenames: list[str] = None) -> Optional[str]:
    """
    使用审核意见模板 DOCX，替换其中的 xxx 占位符生成审核意见。

    **以模板为主**：模板的抬头、措辞、落款格式与页眉图章全部原样保留，
    只做占位符填充，不插入任何 LLM 生成的正文：

    - P1 中所有 xxx → 实际信息
    - 末段日期 → 当前日期

    其余内容（含标题抬头）保持模板原文不动。
    实质分析结论仍由邮件正文/AI分析报告承载，不写入本意见书。
    """
    try:
        from docx import Document
    except ImportError:
        logger.warning("python-docx 未安装，无法生成审核意见 docx")
        return None

    from datetime import datetime
    from pathlib import Path

    tp = Path(template_path)
    if not tp.exists():
        logger.warning(f"审核意见模板不存在: {template_path}")
        return None

    doc = Document(str(tp))

    # ── 从分析结果中提取占位符填充值 ──
    involved = analysis.get("involved_parties", "") or ""
    parties = [p.strip() for p in involved.split(",") if p.strip()]
    party_a = parties[0] if len(parties) >= 1 else "（待确认）"
    party_b = parties[1] if len(parties) >= 2 else "（待确认）"
    case_summary = analysis.get("case_summary", "") or "（待确认）"
    # 从摘要中剥离金额表述，避免与模板自带的"合同价款xxx元"重复
    case_summary_clean = _strip_amount_phrases(case_summary)

    # 从附件文件名提取文书标题，兜底用邮件主题
    contract_name = _extract_doc_title(attachment_filenames) or original_subject or "（待确认）"

    # 尝试从正文/摘要中提取金额
    amount = _extract_amount(body_text or case_summary)

    # ── 解析发件人：提取名称，兜底用邮箱 ──
    import re
    sender_email_match = re.search(r'<([^>]+)>', sender)
    if sender_email_match:
        sender_name = sender[:sender_email_match.start()].strip().strip('"').strip("'").strip()
        sender_email = sender_email_match.group(1).strip()
    else:
        sender_name = ""
        sender_email = sender.strip()
    sender_display = sender_name or sender_email

    # ── P1: 替换 xxx ──
    if len(doc.paragraphs) > 1:
        p1 = doc.paragraphs[1]
        now = datetime.now()
        replacements = [
            (str(now.year),),           # 第1个xxx → 年份
            (str(now.month),),           # 第2个xxx → 月份
            (sender_display,),           # 第3个xxx → 发来方名称
            (party_b,),                  # 第4个xxx → 拟与...签订方
            (contract_name,),            # 第5个xxx → 合同名称
            (case_summary_clean,),       # 第6个xxx → 合同内容（已剥离金额）
            (amount,),                   # 第7个xxx → 价款
        ]
        _replace_xxx_in_paragraph(p1, replacements)

    # ── 模板自带的律所措辞与抬头，保持不动 ──

    # ── 落款日期：替换模板的示例日期 ──
    if doc.paragraphs:
        p_date = datetime.now().strftime("%Y年%m月%d日")
        _replace_paragraph_text(doc.paragraphs[-1], p_date)

    # 写入临时文件
    tmp = tempfile.NamedTemporaryFile(
        suffix=".docx", prefix="审核意见_", delete=False
    )
    doc.save(tmp.name)
    logger.info(f"审核意见已生成: {tmp.name} (模板: {tp.name})")
    return tmp.name


def _extract_amount(text: str) -> str:
    """从文本中提取金额，返回**纯数值串**（去掉货币符号与末尾「元」）或默认值。

    模板自带「合同价款xxx元」的「元」字，故返回值不再带单位；
    `￥`/`¥`/`人民币` 等前缀同样由模板承担，不重复带入。
    注意字符类里**不能**包含 `,` 与 `.` 以外的贪婪写法：也不要写成
    `\\d+[\\d,.]*`——那会在 `人民币12000元` 上从第二个数字起匹配，
    把「人民币」留成残渣。
    """
    import re
    # 匹配模式：XXX万元 / XXX元 / 人民币XXX元 / ￥XXX 等
    patterns = [
        r'(?:人民币\s*)?\d[\d,]*\.?\d*\s*万?\s*元',
        r'[¥￥]\s*\d[\d,]*\.?\d*\s*万?',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            val = m.group(0).strip()
            val = re.sub(r'^人民币\s*', '', val)                # 去掉「人民币」前缀
            val = re.sub(r'^[¥￥]\s*', '', val)                 # 去掉货币符号
            val = re.sub(r'\s*万\s*$', '万', val.strip())       # 归一化「30 万」→「30万」
            if val.endswith("元"):                              # 模板自带「元」，避免重复
                val = val[:-1]
            val = val.strip()
            if val:
                return val
    return "（待确认）"


def _strip_amount_phrases(text: str) -> str:
    """从文本中剥离金额相关表述，避免与模板自带的价款字段重复"""
    import re
    # 匹配模式：金额表述 + 可选的前后标点/空格
    patterns = [
        r'，?\s*合同总?金额[约共]?(人民币\s*)?(\d[\d,]*\.?\d*\s*万?\s*元)[。，]?\s*',  # "，合同总金额30万元。"
        r'，?\s*总?金额[约共]?(人民币\s*)?(\d[\d,]*\.?\d*\s*万?\s*元)[。，]?\s*',     # "，总金额30万元。"
        r'，?\s*价款[约共]?(人民币\s*)?(\d[\d,]*\.?\d*\s*万?\s*元)[。，]?\s*',         # "，价款30万元。"
        r'，?\s*(¥|￥)\s*(\d[\d,]*\.?\d*\s*万?)[。，]?\s*',                           # "，¥30万。"
    ]
    result = text
    for pat in patterns:
        result = re.sub(pat, '', result)
    return result.strip().rstrip('，。').strip()


def _replace_xxx_in_paragraph(paragraph, replacements: list[tuple]):
    """
    按顺序替换段落中的 xxx 占位符。

    replacements: [(val1, fallback1), (val2,), ...]
    每个xxx依次被对应值替换；None/空值使用fallback或"（待确认）"。
    """
    if not paragraph.runs:
        return

    # 收集所有 run 的文本，构建完整段落文本
    full_text = paragraph.text
    for repl in replacements:
        if isinstance(repl, tuple):
            val = repl[0] or ""
            if not val and len(repl) > 1:
                val = repl[1] or ""
        else:
            val = str(repl) if repl else ""
        if not val:
            val = "（待确认）"
        # 替换第一个 xxx
        full_text = full_text.replace("xxx", str(val), 1)

    # 写回段落：保留第一个 run 格式，设置全部文本
    if paragraph.runs:
        for run in paragraph.runs[1:]:
            run.text = ""
        paragraph.runs[0].text = full_text


def _replace_paragraph_text(paragraph, new_text: str):
    """替换段落的文本内容，保留第一个 run 的格式"""
    if not paragraph.runs:
        # 无 runs → 直接设 text 可能丢格式，追加一个 run
        paragraph.add_run(new_text)
        return
    # 清空所有 run 的文本，仅保留第一个 run 并设置新文本
    for run in paragraph.runs[1:]:
        run.text = ""
    paragraph.runs[0].text = new_text
