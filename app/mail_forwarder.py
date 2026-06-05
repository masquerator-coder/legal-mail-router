"""
邮件转发模块 — SMTP 转发到目标律师
"""
import smtplib
import logging
import tempfile
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from pathlib import Path
from typing import Optional
from app.config import decrypt

logger = logging.getLogger(__name__)


def _safe_pct(val) -> str:
    """安全的百分比格式化"""
    try:
        return f"{float(val or 0):.0%}"
    except (ValueError, TypeError):
        return "N/A"


def _build_email_body(
    to_name: str,
    analysis: dict,
    original_subject: str,
    original_body: str = "",
    brief_mode: bool = False,
) -> str:
    """构建邮件正文（支持完整版和摘要版）"""
    urgency_map = {"high": "🔴 紧急", "medium": "🟡 一般", "low": "🟢 普通"}
    urgency_text = urgency_map.get(analysis.get("urgency"), "\U0001f7e1 一般")

    # 原邮件正文（截断过长内容）
    body_display = original_body or "（无正文）"
    if len(body_display) > 3000:
        body_display = body_display[:3000] + "\n... (原文过长已截断，请登录监控邮箱查看完整内容)"

    if brief_mode:
        # 摘要版：仅展示概要信息，提示查看附件
        return f"""您好 {to_name}，

系统收到一封法律文书邮件，AI 分析结果如下：

━━━━━━━━━━━━━━━━━━━━
\U0001f4cb 文书类型：{analysis.get('doc_type', '未知')}
\u26a1 紧急程度：{urgency_text}
\U0001f4dd 案件摘要：{analysis.get('case_summary', '无')}
\U0001f3db\ufe0f 涉及方：{analysis.get('involved_parties', '无')}
\U0001f4c5 关键日期：{analysis.get('key_date', '无')}
\U0001f4ce 案号：{analysis.get('case_number', '无')}
\U0001f4ca 分析置信度：{_safe_pct(analysis.get('confidence'))}
━━━━━━━━━━━━━━━━━━━━

📎 AI 初步审核解读详见附件《AI分析报告.docx》—— 请下载查阅完整解读内容。

原邮件主题：{original_subject}

━━━━━━━━━━━━━━━━━━━━
\U0001f4e7 原邮件正文：
{body_display}
━━━━━━━━━━━━━━━━━━━━

此为自动转发，如需查看完整原始邮件请登录监控邮箱。

---
文书自动分拣系统
"""

    # 完整版正文（含AI解读）
    ai_interp = analysis.get('ai_interpretation', '')
    if not ai_interp:
        ai_interp = '（暂无AI解读，请人工审核）'

    return f"""您好 {to_name}，

系统收到一封法律文书邮件，AI 分析结果如下：

━━━━━━━━━━━━━━━━━━━━
\U0001f4cb 文书类型：{analysis.get('doc_type', '未知')}
\u26a1 紧急程度：{urgency_text}
\U0001f4dd 案件摘要：{analysis.get('case_summary', '无')}
\U0001f3db\ufe0f 涉及方：{analysis.get('involved_parties', '无')}
\U0001f4c5 关键日期：{analysis.get('key_date', '无')}
\U0001f4ce 案号：{analysis.get('case_number', '无')}
\U0001f4ca 分析置信度：{_safe_pct(analysis.get('confidence'))}
━━━━━━━━━━━━━━━━━━━━

\U0001f916 AI 初步审核解读：
{ai_interp}

━━━━━━━━━━━━━━━━━━━━

原邮件主题：{original_subject}

━━━━━━━━━━━━━━━━━━━━
\U0001f4e7 原邮件正文：
{body_display}
━━━━━━━━━━━━━━━━━━━━

此为自动转发，如需查看完整原始邮件请登录监控邮箱。

---
文书自动分拣系统
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


def _generate_analysis_docx(analysis: dict, original_subject: str) -> Optional[str]:
    """
    将 AI 分析结果生成为 .docx 文件，返回文件路径。

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

    # ── 标题 ──
    title = doc.add_heading("AI 初步审核解读报告", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    # ── 基本信息表 ──
    doc.add_heading("基本信息", level=1)
    table = doc.add_table(rows=8, cols=2, style="Light Grid Accent 1")
    table.autofit = True

    urgency_map = {"high": "🔴 紧急", "medium": "🟡 一般", "low": "🟢 普通"}
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
        # 加粗第一列
        for paragraph in row.cells[0].paragraphs:
            for run in paragraph.runs:
                run.bold = True

    # ── AI 解读正文 ──
    doc.add_heading("AI 初步审核解读", level=1)
    ai_interp = analysis.get("ai_interpretation", "")
    if ai_interp:
        # 双重保障：清理可能残留的 markdown 标记
        ai_interp = _strip_markdown(ai_interp)
        for line in ai_interp.split("\n"):
            p = doc.add_paragraph(line.strip())
            p.paragraph_format.space_after = Pt(4)
            p.paragraph_format.line_spacing = 1.35
    else:
        doc.add_paragraph("（暂无AI解读，请人工审核）")

    # ── 尾部信息 ──
    doc.add_paragraph("")
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = p.add_run("—— 文书自动分拣系统 自动生成 ——")
    run.font.size = Pt(9)
    run.font.color.rgb = RGBColor(128, 128, 128)

    # 写入临时文件
    tmp = tempfile.NamedTemporaryFile(
        suffix=".docx", prefix="ai_analysis_", delete=False
    )
    doc.save(tmp.name)
    logger.info(f"AI 分析报告已生成: {tmp.name}")
    return tmp.name


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
    analysis_result: dict = None,
    attachment_paths: list[str] = None,
    analysis_output_mode: str = "content",
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
        analysis_result: LLM 分析结果 (含 ai_interpretation)
        attachment_paths: 附件路径列表
        analysis_output_mode: 输出模式 — "content"=邮件正文, "attachment"=Word附件

    返回: (成功, 错误信息) — 失败时错误信息包含详细原因
    """
    smtp_password = decrypt(smtp_password_encrypted)

    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email
    # 安全检查：analysis_result 可能为 None（关键词匹配路径）
    analysis = analysis_result or {}
    llm_failed = analysis.get("llm_failed", False)
    if llm_failed:
        msg["Subject"] = f"【大模型分析失败】{original_subject}"
    else:
        msg["Subject"] = f"【{analysis.get('doc_type', '法律文书')}】{original_subject}"
    msg["X-Forwarded-By"] = "文书分拣系统"
    msg["X-Forwarded-For"] = from_email

    # ── 构建正文 ──
    if analysis_output_mode == "attachment":
        # 附件模式：生成 Word 文档作为附件，正文仅保留摘要
        docx_path = _generate_analysis_docx(analysis, original_subject)
        body = _build_email_body(
            to_name, analysis, original_subject, original_body, brief_mode=True
        )
        msg.attach(MIMEText(body, "plain", "utf-8"))

        if docx_path:
            path = Path(docx_path)
            with open(path, "rb") as f:
                part = MIMEApplication(f.read(), Name="AI分析报告.docx")
                part["Content-Disposition"] = 'attachment; filename="AI分析报告.docx"'
                msg.attach(part)
    else:
        # 默认：邮件正文模式（完整版本）
        body = _build_email_body(
            to_name, analysis, original_subject, original_body, brief_mode=False
        )
        msg.attach(MIMEText(body, "plain", "utf-8"))

    # ── 原邮件附件 ──
    if attachment_paths:
        for file_path in attachment_paths:
            path = Path(file_path)
            if not path.exists():
                logger.warning(f"附件不存在: {file_path}")
                continue
            with open(path, "rb") as f:
                part = MIMEApplication(f.read(), Name=path.name)
                part["Content-Disposition"] = f'attachment; filename="{path.name}"'
                msg.attach(part)

    # 发送
    server = None
    try:
        if smtp_port == 465:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=30)
            server.starttls()

        server.login(smtp_username, smtp_password)
        server.sendmail(from_email, [to_email], msg.as_string())

        logger.info(f"邮件已转发: {original_subject} → {to_email}")
        return True, None

    except smtplib.SMTPAuthenticationError as e:
        error_msg = f"认证失败：账号或授权码错误 (SMTP AUTH 报错: {e})"
        logger.error(f"转发邮件认证失败: {error_msg}")
        return False, error_msg

    except smtplib.SMTPRecipientsRefused as e:
        error_msg = f"收件人被拒绝：{to_email} (SMTP 报错: {e})"
        logger.error(f"转发邮件收件人被拒绝: {error_msg}")
        return False, error_msg

    except smtplib.SMTPDataError as e:
        error_msg = f"邮件数据被拒绝：服务器拒收邮件内容 (SMTP 报错: {e})"
        logger.error(f"转发邮件数据错误: {error_msg}")
        return False, error_msg

    except smtplib.SMTPConnectError as e:
        error_msg = f"连接失败：无法连接到 {smtp_host}:{smtp_port} (SMTP 报错: {e})"
        logger.error(f"转发邮件连接失败: {error_msg}")
        return False, error_msg

    except smtplib.SMTPSenderRefused as e:
        error_msg = f"发件人被拒绝：{from_email} (SMTP 报错: {e})"
        logger.error(f"转发邮件发件人被拒绝: {error_msg}")
        return False, error_msg

    except smtplib.SMTPServerDisconnected as e:
        error_msg = f"服务器断开：{smtp_host} 在转发过程中断开连接 (SMTP 报错: {e})"
        logger.error(f"转发邮件服务器断开: {error_msg}")
        return False, error_msg

    except smtplib.SMTPException as e:
        error_msg = f"SMTP 错误：{smtp_host}:{smtp_port} (类型: {type(e).__name__}, 详情: {e})"
        logger.error(f"转发邮件 SMTP 异常: {error_msg}")
        return False, error_msg

    except TimeoutError:
        error_msg = f"超时：连接 {smtp_host}:{smtp_port} 超过 30 秒无响应"
        logger.error(f"转发邮件超时: {error_msg}")
        return False, error_msg

    except ConnectionError as e:
        error_msg = f"网络错误：无法连接到 {smtp_host}:{smtp_port} (详情: {e})"
        logger.error(f"转发邮件网络错误: {error_msg}")
        return False, error_msg

    except Exception as e:
        error_msg = f"未知错误：转发到 {to_email} 失败 (类型: {type(e).__name__}, 详情: {e})"
        logger.error(f"转发邮件失败: {error_msg}")
        return False, error_msg

    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass


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


# ── 修改版文书 docx 生成 ──

def _parse_revision_markers(text: str) -> list[tuple[str, str]]:
    """将含 COLLABORATION 标记的修订文本解析为 (tag, text) 序列。

    tag: "normal" | "add" | "modify" | "delete"
    """
    import re
    pattern = r'【(新增|修改|删除)】(.*?)【/\1】'
    segments = []
    last_end = 0

    for m in re.finditer(pattern, text, re.DOTALL):
        if m.start() > last_end:
            segments.append(("normal", text[last_end:m.start()]))
        tag_map = {"新增": "add", "修改": "modify", "删除": "delete"}
        segments.append((tag_map[m.group(1)], m.group(2)))
        last_end = m.end()

    if last_end < len(text):
        segments.append(("normal", text[last_end:]))

    return segments if segments else [("normal", text)]


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

    if use_highlight:
        segments = _parse_revision_markers(revision_text)
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
        # 无色彩模式：直接输出全文（含原始标记，由用户自行阅读）
        for line in revision_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            p = doc.add_paragraph(line.strip())
            p.paragraph_format.space_after = Pt(4)
            p.paragraph_format.line_spacing = 1.35

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
    run_footer = footer.add_run("—— 文书自动分拣系统 自动生成 ——")
    run_footer.font.size = Pt(9)
    run_footer.font.color.rgb = RGBColor(128, 128, 128)

    # 写入临时文件
    tmp = tempfile.NamedTemporaryFile(
        suffix=".docx", prefix="revision_", delete=False
    )
    doc.save(tmp.name)
    logger.info(f"修改版文书已生成: {tmp.name} ({len(revision_text)} 字符)")
    return tmp.name


def _fill_review_template(template_path: str, analysis: dict,
                          original_subject: str, sender: str = "",
                          body_text: str = "") -> Optional[str]:
    """
    使用审核意见模板 DOCX，替换其中的 xxx 占位符生成审核意见。

    仅替换：
    - P1 中所有 xxx → 实际信息
    - P6 日期 → 当前日期
    其余内容保持模板原文不动。
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
    contract_name = original_subject or "（待确认）"

    # 尝试从正文/摘要中提取金额
    amount = _extract_amount(body_text or case_summary)

    # ── P1: 替换 xxx ──
    if len(doc.paragraphs) > 1:
        p1 = doc.paragraphs[1]
        replacements = [
            (party_a, sender),          # 第1个xxx → 发来方
            (party_b,),                 # 第2个xxx → 拟与...签订方
            (contract_name,),           # 第3个xxx → 合同名称
            (case_summary_clean,),      # 第4个xxx → 合同内容（已剥离金额）
            (amount,),                  # 第5个xxx → 价款
        ]
        _replace_xxx_in_paragraph(p1, replacements)

    # ── P2: 保持不动（模板已有律所措辞）──

    # ── P6: 替换日期 ──
    if len(doc.paragraphs) > 6:
        p6_text = datetime.now().strftime("%Y年%m月%d日")
        _replace_paragraph_text(doc.paragraphs[6], p6_text)

    # 写入临时文件
    tmp = tempfile.NamedTemporaryFile(
        suffix=".docx", prefix="review_opinion_", delete=False
    )
    doc.save(tmp.name)
    logger.info(f"审核意见已生成: {tmp.name} (模板: {tp.name})")
    return tmp.name


def _extract_amount(text: str) -> str:
    """从文本中提取金额，返回格式化字符串或默认值"""
    import re
    # 匹配模式：XXX万元 / XXX元 / 人民币XXX元 等
    patterns = [
        r'(人民币\s*)?(\d+[\d,.]*\s*万?\s*元)',
        r'(¥|￥)\s*(\d+[\d,.]*\s*万?)',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            val = m.group(0).strip()
            # 模板中已有 "元"，去掉匹配到的末尾 "元" 避免重复
            if val.endswith("元"):
                val = val[:-1]
            return val
    return "（待确认）"


def _strip_amount_phrases(text: str) -> str:
    """从文本中剥离金额相关表述，避免与模板自带的价款字段重复"""
    import re
    # 匹配模式：金额表述 + 可选的前后标点/空格
    patterns = [
        r'，?\s*合同总?金额[约共]?(人民币\s*)?(\d+[\d,.]*\s*万?\s*元)[。，]?\s*',  # "，合同总金额30万元。"
        r'，?\s*总?金额[约共]?(人民币\s*)?(\d+[\d,.]*\s*万?\s*元)[。，]?\s*',     # "，总金额30万元。"
        r'，?\s*价款[约共]?(人民币\s*)?(\d+[\d,.]*\s*万?\s*元)[。，]?\s*',         # "，价款30万元。"
        r'，?\s*(¥|￥)\s*(\d+[\d,.]*\s*万?)[。，]?\s*',                           # "，¥30万。"
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
