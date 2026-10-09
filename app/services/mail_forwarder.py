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


# ════════════════════════════════════════════
# 审查意见模板：按文书类型名约定式查找
# ════════════════════════════════════════════
# 模板文件名与文书类型名**逐字一致**：templates/<文书类型>审核意见模板.docx
# （如「合同协议」→ templates/合同协议审核意见模板.docx）。
# 无对应模板文件的类型不生成审查意见，只出分析报告。
_REVIEW_TEMPLATE_DIR = "templates"
_REVIEW_TEMPLATE_SUFFIX = "审核意见模板.docx"

# 占位符缺值时的填充文本
_MISSING_VALUE = "（待确认）"


def _review_template_path(doc_type: str | None) -> Optional[Path]:
    """按文书类型名解析模板路径；无对应模板文件时返回 None。

    约定：templates/<文书类型>审核意见模板.docx。不设别名表——
    文件名与类型名不一致即视为该类型未配备模板（记 WARNING，跳过生成）。
    """
    from app.config import BASE_DIR

    name = (doc_type or "").strip()
    if not name:
        return None
    p = BASE_DIR / _REVIEW_TEMPLATE_DIR / f"{name}{_REVIEW_TEMPLATE_SUFFIX}"
    return p if p.exists() else None


def _fill_review_template(template_path: str, analysis: dict,
                          original_subject: str, sender: str = "",
                          body_text: str = "",
                          attachment_filenames: list[str] = None) -> Optional[str]:
    """
    使用审核意见模板 DOCX，替换其中的**命名占位符**生成审核意见。

    **以模板为主**：模板的抬头、措辞、落款格式与页眉图章全部原样保留，
    只做占位符填充，不插入任何 LLM 生成的正文。实质分析结论仍由
    邮件正文/AI分析报告承载，不写入本意见书。

    占位符（按名称替换，与所在段落、出现次序无关）：
    - 【收到邮件日期】/ `xxxx年x月x日` → 生成当天日期
    - 其余【…】占位符 → 由 `analysis` 中的同义字段填充（见 `_placeholder_values`）
    - 末段日期 → 生成当天日期

    三份模板的段落布局不同（信访件在 P1、政府信息公开在 P2），故填充
    **不依赖段落序号**，仅按占位符名称定位。
    """
    try:
        from docx import Document
    except ImportError:
        logger.warning("python-docx 未安装，无法生成审核意见 docx")
        return None

    from pathlib import Path

    tp = Path(template_path)
    if not tp.exists():
        logger.warning(f"审核意见模板不存在: {template_path}")
        return None

    doc = Document(str(tp))
    values = _placeholder_values(analysis, original_subject, sender, body_text)

    # ── 正文：按名称替换【…】占位符与 xxxx年x月x日 ──
    filled = set()
    for para in doc.paragraphs:
        filled |= _fill_placeholders_in_paragraph(para, values)

    # ── 落款日期：按日期样式定位（不依赖段序）──
    _fill_signature_date(doc, values["收到邮件日期"])

    # 占位符残留检查：模板与填充值不匹配时留痕，避免静默生成半成品
    leftover = _find_leftover_placeholders(doc)
    if leftover:
        logger.warning(
            f"审核意见模板 {tp.name} 存在未填充占位符: {sorted(leftover)}"
        )

    # 写入临时文件
    tmp = tempfile.NamedTemporaryFile(
        suffix=".docx", prefix="审核意见_", delete=False
    )
    doc.save(tmp.name)
    logger.info(
        f"审核意见已生成: {tmp.name} (模板: {tp.name}, 填充 {len(filled)} 个占位符)"
    )
    return tmp.name


def _placeholder_values(analysis: dict, original_subject: str = "",
                        sender: str = "", body_text: str = "") -> dict:
    """构建占位符名 → 填充值 的映射（缺失值统一为「（待确认）」）。

    取值全部来自 LLM 对**送审文书正文**的分析字段，**不使用邮件标题**，
    也不使用附件文件名——这两者常与文书正文标题/当事人不一致。
    """
    a = analysis or {}

    def pick(*keys) -> str:
        for k in keys:
            v = a.get(k)
            if v is None:
                continue
            s = str(v).strip()
            # 模型未提取到时可能返回这些字面量，一律视为缺失
            if s and s.lower() not in ("null", "none", "无", "未知"):
                return s
        return _MISSING_VALUE

    today = datetime.now()
    date_str = today.strftime("%Y年%m月%d日")

    return {
        "收到邮件日期": date_str,
        "合同甲方": pick("contract_party_a"),
        "合同相对方": pick("contract_party_b"),
        "合同正文名称": pick("contract_name"),
        "合同内容": pick("contract_content"),
        "合同价款": pick("contract_amount", "contract_price"),
        "行政机关": pick("agency_name"),
        "被申请人": pick("agency_name"),
        "申请文件名称及文号": pick("document_title_no"),
    }


def _fill_placeholders_in_paragraph(paragraph, values: dict) -> set:
    """在单个段落内按名称替换所有占位符，返回本次实际填充的占位符名集合。

    模板把文字拆成多个 run（字体/修订标记所致），故先合并出整段文本，
    再一次性写回**首个 run**（沿用仓库既有的「首 run 承载全文」手法），
    以保留原字体与段落格式。
    """
    import re

    if not paragraph.runs:
        return set()

    text = paragraph.text
    if not text:
        return set()

    filled = set()

    # 1) 命名占位符：【xxx】
    def _sub(m):
        name = m.group(1)
        val = values.get(name)
        if val is None:
            return m.group(0)          # 未知占位符原样保留，由残留检查报告
        filled.add(name)
        return val

    text = re.sub(r"【([^】]{1,30})】", _sub, text)

    # 2) 日期占位符：xxxx年x月x日 / xxx年xxx月（兼容旧写法）
    if re.search(r"x{2,}年x{1,2}月x{1,2}日", text):
        text = re.sub(r"x{2,}年x{1,2}月x{1,2}日", values["收到邮件日期"], text)
        filled.add("收到邮件日期")
    elif re.search(r"x{2,}年x{2,}月", text):
        text = re.sub(r"x{2,}年x{2,}月", values["收到邮件日期"], text)
        filled.add("收到邮件日期")

    if not filled:
        return set()

    for run in paragraph.runs[1:]:
        run.text = ""
    paragraph.runs[0].text = text
    return filled


def _fill_signature_date(doc, date_str: str):
    """写入落款日期：按**段落形态**定位，不依赖固定段序。

    定位顺序（只认「空段」或「纯日期段」，绝不改写正文）：
    1. 正文中已含日期文字的段落（旧模板的 `2023年1月16日`）；
    2. 文档**尾部**的空段落——优先右对齐（落款惯例），否则取最后一个空段。

    三份模板的落款位形态不一（合同协议有右对齐空段、信访件全是普通空段），
    故不能用 alignment 是否为 None 来筛（该属性常为继承值）。
    找不到可用段落时跳过并记日志，**不误伤正文**。
    """
    import re

    date_re = re.compile(r"^\s*\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*$")

    def _write(p) -> None:
        for run in p.runs[1:]:
            run.text = ""
        if p.runs:
            p.runs[0].text = date_str
        else:
            p.add_run(date_str)

    paras = list(doc.paragraphs)

    # 1) 已含「纯日期」文字的段落
    for p in paras:
        if date_re.match(p.text or ""):
            _write(p)
            return True

    # 2) 尾部空段落：先找右对齐的，再退化为最后一个空段
    blanks = [p for p in paras if not (p.text or "").strip()]
    right_blanks = [p for p in blanks if p.alignment is not None and str(p.alignment).startswith("RIGHT")]
    target = right_blanks[-1] if right_blanks else (blanks[-1] if blanks else None)
    if target is not None:
        _write(target)
        return True

    logger.warning("审核意见模板未找到落款日期段落，已跳过日期填充")
    return False


def _find_leftover_placeholders(doc) -> set:
    """扫描生成结果中残留的占位符（【…】或 `xxx`），用于填充异常留痕。"""
    import re

    leftover = set()
    for p in doc.paragraphs:
        t = p.text or ""
        leftover |= set(re.findall(r"【([^】]{1,30})】", t))
        if re.search(r"x{2,}年x{1,2}月", t):
            leftover.add("xxxx年x月x日")
        if "xxx" in t:
            leftover.add("xxx")
    return leftover
