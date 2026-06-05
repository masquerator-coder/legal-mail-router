"""
调度引擎 — APScheduler 管理定时邮件检查任务
"""
import logging
from typing import Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

import threading
import json
import copy
import time
import os
from datetime import datetime, timedelta
import smtplib
from email.mime.text import MIMEText
from sqlalchemy import func

logger = logging.getLogger(__name__)

# 全局调度器实例
scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

# 账号级别线程锁（防并发触发）
_account_locks = {}
_account_locks_lock = threading.Lock()

# 全局进度状态（供 Web UI 轮询）
_check_progress = {
    "running": False,
    "step": "idle",       # idle | connecting | scanning | processing | analyzing | forwarding | done | error
    "step_label": "",     # 中文描述
    "total": 0,           # 总邮件数
    "current": 0,         # 当前第几封
    "subject": "",        # 当前邮件主题
    "message": "",        # 额外消息
    "started_at": None,   # 开始时间 ISO
}


def get_progress() -> dict:
    """获取当前执行进度"""
    return copy.deepcopy(_check_progress)


_progress_lock = threading.Lock()

def _update_progress(**kwargs):
    """线程安全更新进度"""
    with _progress_lock:
        _check_progress.update(kwargs)


def _make_job_id(account_id: int) -> str:
    return f"email_check_{account_id}"


def add_check_job(account_id: int, interval_minutes: int):
    """添加或更新邮件检查任务"""
    job_id = _make_job_id(account_id)

    # 移除旧任务
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    scheduler.add_job(
        func=check_account,
        trigger=IntervalTrigger(minutes=interval_minutes),
        args=[account_id],
        id=job_id,
        name=f"检查邮箱 #{account_id}",
        replace_existing=True,
        misfire_grace_time=3600,  # 1小时容错（防止短暂阻塞导致任务丢失）
    )
    logger.info(f"已添加检查任务: {job_id} (间隔 {interval_minutes} 分钟)")


def remove_check_job(account_id: int):
    """移除邮件检查任务"""
    job_id = _make_job_id(account_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info(f"已移除检查任务: {job_id}")


def check_account(account_id: int):
    """检查指定邮箱账户（由调度器调用）"""
    from app.database import SessionLocal

    # 防止同一账户并发执行
    with _account_locks_lock:
        if account_id not in _account_locks:
            _account_locks[account_id] = threading.Lock()
        lock = _account_locks[account_id]

    if not lock.acquire(blocking=False):
        logger.info(f"账户 #{account_id} 正在处理中，跳过本次触发")
        return

    try:
        _update_progress(
            running=True, step="connecting", step_label="正在连接邮箱...",
            total=0, current=0, subject="", message="",
            started_at=datetime.now().isoformat()
        )

        db = SessionLocal()
        try:
            # ── 1. 加载配置 ──
            ctx = _load_context(db, account_id)
            if ctx is None:
                return  # account not found (progress already updated)

            # ── 2. 拉取邮件 + 批量查重 ──
            new_emails, total = _fetch_and_dedup(ctx["account"], db, ctx["monitor_days"])
            if not new_emails:
                _update_progress(running=False, step="done", step_label="无新邮件")
                return

            _update_progress(step="processing", step_label="正在处理邮件...", total=total, current=0)

            # ── 3. 逐封处理 ──
            for idx, eml in enumerate(new_emails, 1):
                _update_progress(current=idx, subject=eml.subject[:60])
                _process_one_email(eml, idx, ctx, db)

        except Exception as e:
            logger.error(f"检查账户 #{account_id} 时出错: {e}", exc_info=True)
            _update_progress(step="error", step_label=f"出错: {str(e)[:80]}", running=False)
            db.rollback()
        finally:
            db.close()
            if _check_progress.get("step") != "error":
                _update_progress(step="done", step_label="处理完成", running=False)
    finally:
        lock.release()


# ── 模块级辅助函数 ──

def _try_match_rules(rules_list: list, analysis_result: dict | None, text: str):
    """尝试在规则列表中匹配路由规则，返回匹配到的规则或 None

    匹配优先级：
    1. LLM 文书类型精确匹配
    2. LLM 律师类型匹配
    3. 模糊子串匹配
    4. 关键词匹配（有/无 LLM 均生效）
    """
    for rule in rules_list:
        rule_keywords = [k.strip().lower() for k in rule.keywords.split(",") if k.strip()]

        # LLM 分析路径
        if analysis_result:
            target_lawyer_type = analysis_result.get("target_lawyer_type", "默认")
            llm_doc_type = analysis_result.get("doc_type", "")
            if (rule.doc_type == llm_doc_type
                or target_lawyer_type.lower() == rule.doc_type.lower()
                or (llm_doc_type and rule.doc_type in llm_doc_type)
                or (llm_doc_type and any(kw in llm_doc_type for kw in rule_keywords))):
                return rule

        # 关键词路径（有/无 LLM 均生效）
        if rule_keywords and any(kw in text for kw in rule_keywords):
            return rule
    return None


def _load_context(db, account_id: int) -> dict | None:
    """加载一次检查所需的所有配置，返回上下文字典"""
    from app.models import EmailAccount, LLMConfig, OCRConfig, RoutingRule, DefaultConfig

    account = db.query(EmailAccount).filter_by(id=account_id, enabled=True).first()
    if not account:
        logger.warning(f"邮箱账户 #{account_id} 不存在或已禁用")
        _update_progress(running=False, step="error", step_label="账户不存在或已禁用")
        return None

    llm_cfg = db.query(LLMConfig).filter_by(is_active=True).first()
    if not llm_cfg:
        logger.warning("没有激活的 LLM 配置，将仅靠关键词匹配路由规则")

    ocr_cfg_row = db.query(OCRConfig).filter_by(is_active=True).first()
    _ocr_cfg = None
    if ocr_cfg_row:
        from app.config import decrypt
        _ocr_cfg = {
            "provider_type": ocr_cfg_row.provider_type,
            "api_url": ocr_cfg_row.api_url,
            "api_key": decrypt(ocr_cfg_row.api_key_encrypted) if ocr_cfg_row.api_key_encrypted else "",
            "model_name": ocr_cfg_row.model_name,
        }

    account_rules = (
        db.query(RoutingRule)
        .filter_by(account_id=account_id, enabled=True)
        .order_by(RoutingRule.priority.desc())
        .all()
    )
    global_rules = (
        db.query(RoutingRule)
        .filter_by(account_id=None, enabled=True)
        .order_by(RoutingRule.priority.desc())
        .all()
    )

    def _read_setting(key: str, default: str = "") -> str:
        cfg = db.query(DefaultConfig).filter_by(key=key).first()
        return cfg.value if cfg and cfg.value else default

    return {
        "account": account,
        "llm_cfg": llm_cfg,
        "ocr_cfg": _ocr_cfg,
        "account_rules": account_rules,
        "global_rules": global_rules,
        "monitor_days": int(_read_setting("monitor_days", "7")),
        "llm_retry_interval": int(_read_setting("llm_retry_interval", "10")),
        "llm_max_retries": int(_read_setting("llm_max_retries", "3")),
        "revision_enabled": _read_setting("revision_enabled", "false") == "true",
        "revision_prompt": _read_setting("revision_prompt", ""),
        "revision_highlight": _read_setting("revision_highlight", "true") == "true",
        "context_window_tokens": _read_setting("context_window_tokens", "0"),
        "review_template_enabled": _read_setting("review_template_enabled", "false") == "true",
        "review_template_path": _read_setting("review_template_path", "templates/合同审核意见模板.docx"),
    }


def _fetch_and_dedup(account, db, monitor_days: int) -> tuple[list, int]:
    """拉取邮件并批量查重，返回 (新邮件列表, 新邮件总数)"""
    from app.email_fetcher import fetch_new_emails
    from app.models import EmailLog

    _update_progress(step="scanning", step_label="正在扫描邮件列表...")
    emails = fetch_new_emails(
        imap_host=account.imap_host,
        imap_port=account.imap_port,
        username=account.username,
        password_encrypted=account.password_encrypted,
        provider_type=account.provider_type,
        use_ssl=account.use_ssl,
        days=monitor_days,
        filter_sender=account.filter_sender,
        download_attachments=account.download_attachments,
    )
    if not emails:
        return [], 0

    batch_msg_ids = [eml.message_id for eml in emails if eml.message_id]
    existing_ids: set = set()
    if batch_msg_ids:
        existing_ids = set(
            row[0] for row in db.query(EmailLog.message_id)
            .filter(EmailLog.message_id.in_(batch_msg_ids))
            .all()
        )
    new_emails = [eml for eml in emails if eml.message_id not in existing_ids]
    return new_emails, len(new_emails)


def _process_one_email(eml, idx: int, ctx: dict, db):
    """处理单封邮件：保存附件 → LLM 分析 → 路由匹配 → 转发"""
    from app.models import EmailLog, Attachment, DefaultConfig
    from app.email_fetcher import save_attachments, extract_attachment_texts
    from app.mail_forwarder import forward_email, get_default_smtp_config

    account = ctx["account"]
    llm_cfg = ctx["llm_cfg"]

    # 跳过已被转发的副本
    if eml.subject.strip().startswith("【"):
        logger.info(f"跳过转发副本: {eml.subject}")
        return

    # ── 创建日志记录 ──
    log = EmailLog(
        account_id=account.id,
        message_id=eml.message_id,
        subject=eml.subject,
        sender=eml.sender,
        received_at=eml.date,
        body_preview=eml.body_text[:500],
        body_text=eml.body_text,
        status="pending",
    )
    db.add(log)
    db.flush()

    # ── 保存附件 ──
    attachment_records = []
    if account.download_attachments and eml.attachments:
        attachment_records = save_attachments(eml, log.id, account.username)
        for att in attachment_records:
            db.add(Attachment(
                log_id=log.id,
                filename=att["filename"],
                file_path=att["file_path"],
                file_size=att["file_size"],
            ))
        db.flush()
    db.commit()  # 提前提交，缩短事务窗口

    # ── 提取附件文本 ──
    attachment_texts = ""
    unocr_images = []
    if eml.attachments:
        attachment_texts, unocr_images = extract_attachment_texts(eml, ocr_cfg=ctx["ocr_cfg"])
        if attachment_texts:
            logger.info(f"已提取 {len(eml.attachments)} 个附件文本 ({len(attachment_texts)} 字符)")
        if unocr_images:
            logger.info(f"有 {len(unocr_images)} 张图片未被 OCR 处理，将尝试多模态降级")

    # ── LLM 分析 ──
    analysis, llm_failed = _run_llm_analysis(
        llm_cfg=llm_cfg,
        eml=eml,
        log=log,
        attachment_texts=attachment_texts,
        unocr_images=unocr_images,
        account_rules=ctx["account_rules"],
        global_rules=ctx["global_rules"],
        retry_interval=ctx["llm_retry_interval"],
        max_retries=ctx["llm_max_retries"],
    )

    # ── 🆕 修改版文书生成 ──
    revision_path = None
    if (ctx.get("revision_enabled")
            and not llm_failed
            and analysis
            and llm_cfg):
        # 计算有效的上下文窗口
        from app.llm_analyzer import get_effective_context_window
        from app.config import decrypt
        context_window = get_effective_context_window(
            ctx.get("context_window_tokens", "0"),
            llm_cfg.api_url,
            decrypt(llm_cfg.api_key_encrypted) if llm_cfg.api_key_encrypted else "",
            llm_cfg.model_name,
        )
        # 原文截断 = 上下文窗口的 45%，修订输出 = 5%
        original_truncation = int(context_window * 0.45)
        revision_max_tokens = int(context_window * 0.05)
        # 确保在合理范围
        revision_max_tokens = max(1000, min(revision_max_tokens, 16000))

        original_text = (attachment_texts if attachment_texts else eml.body_text)[:original_truncation]

        # 加载文书模板
        template = _load_doc_template(db, analysis.get("doc_type", ""), ctx["account"].id)

        try:
            from app.email_fetcher import _run_async_safe
            from app.llm_analyzer import generate_revision
            from app.mail_forwarder import _generate_revision_docx

            _update_progress(step="analyzing", step_label="正在生成修改版文书...")
            revision_text = _run_async_safe(
                generate_revision(
                    api_url=llm_cfg.api_url,
                    api_key_encrypted=llm_cfg.api_key_encrypted,
                    model_name=llm_cfg.model_name,
                    doc_type=analysis.get("doc_type", "其他法律文书"),
                    original_text=original_text,
                    ai_interpretation=analysis.get("ai_interpretation", ""),
                    custom_prompt=ctx.get("revision_prompt", ""),
                    template=template,
                    max_tokens=revision_max_tokens,
                    temperature=llm_cfg.temperature,
                )
            )
            if revision_text:
                revision_path = _generate_revision_docx(
                    revision_text=revision_text,
                    doc_type=analysis.get("doc_type", "其他法律文书"),
                    original_subject=eml.subject,
                    use_highlight=ctx.get("revision_highlight", True),
                )
                if revision_path:
                    import os
                    attachment_records.append({
                        "filename": f"修改版文书-{analysis.get('doc_type', '文书')}.docx",
                        "file_path": revision_path,
                        "file_size": os.path.getsize(revision_path),
                    })
                    log.ai_interpretation = (log.ai_interpretation or "") + (
                        "\n\n📎 已生成修改版文书，请参见附件「修改版文书.docx」。"
                    )
        except Exception as e:
            logger.error(f"修改版文书生成失败: [{type(e).__name__}] {e}")
            # 不阻断转发流程

    # ── 🆕 审核意见模板生成 ──
    review_template_path = None
    if (ctx.get("review_template_enabled")
            and not llm_failed
            and analysis
            and ctx.get("review_template_path")):
        try:
            from app.mail_forwarder import _fill_review_template
            from app.config import BASE_DIR

            template_full_path = BASE_DIR / ctx["review_template_path"]
            _update_progress(step="analyzing", step_label="正在生成审核意见...")
            review_template_path = _fill_review_template(
                template_path=str(template_full_path),
                analysis=analysis,
                original_subject=eml.subject,
                sender=eml.sender,
                body_text=attachment_texts or eml.body_text,
            )
            if review_template_path:
                import os as _os
                attachment_records.append({
                    "filename": "审核意见.docx",
                    "file_path": review_template_path,
                    "file_size": _os.path.getsize(review_template_path),
                })
                log.ai_interpretation = (log.ai_interpretation or "") + (
                    "\n\n📎 审核意见书已附，请查阅。"
                )
        except Exception as e:
            logger.error(f"审核意见模板生成失败: [{type(e).__name__}] {e}")

    # ── 路由匹配 ──
    text_to_match = f"{eml.subject} {eml.body_text[:1000]} {attachment_texts[:2000]}".lower()
    matched_rule = None

    if ctx["account_rules"]:
        matched_rule = _try_match_rules(ctx["account_rules"], analysis, text_to_match)
        if matched_rule:
            logger.info(f"命中账户专属规则: {matched_rule.doc_type}")

    if not matched_rule and ctx["global_rules"]:
        matched_rule = _try_match_rules(ctx["global_rules"], analysis, text_to_match)
        if matched_rule:
            logger.info(f"命中全局规则: {matched_rule.doc_type}")

    # 关键词匹配但无 LLM 分析 → 构造最小分析结果
    if matched_rule and not analysis:
        analysis = {
            "doc_type": matched_rule.doc_type,
            "case_summary": f"关键词匹配: {matched_rule.keywords}",
            "ai_interpretation": "", "urgency": "medium",
            "key_date": "", "case_number": "",
            "target_lawyer_type": matched_rule.doc_type,
            "involved_parties": "", "confidence": 0.0,
        }
        log.doc_type = matched_rule.doc_type
        log.case_summary = analysis["case_summary"]
        log.urgency = "medium"

    # LLM 失败但有关键词匹配 → 覆盖文书类型
    if matched_rule and analysis and analysis.get("llm_failed"):
        analysis["doc_type"] = matched_rule.doc_type
        analysis["case_summary"] = f"大模型分析失败，关键词匹配: {matched_rule.keywords}"
        log.doc_type = matched_rule.doc_type
        log.case_summary = analysis["case_summary"]

    # ── 转发决策 ──
    smtp_cfg, forward_target, forward_name = _resolve_forward_target(
        matched_rule=matched_rule,
        analysis=analysis,
        llm_failed=llm_failed,
        account=account,
        db=db,
        log=log,
    )
    if forward_target is None:
        return  # skipped or failed, already committed

    # ── 执行 SMTP 转发 ──
    smtp_cfg = smtp_cfg or get_default_smtp_config(db)
    if smtp_cfg:
        _update_progress(step="forwarding", step_label=f"正在转发到 {forward_target}...")
        from app.config import ATTACHMENTS_DIR
        full_attachment_paths = [
            str(ATTACHMENTS_DIR.parent / att["file_path"]) for att in attachment_records
        ]
        output_mode_cfg = db.query(DefaultConfig).filter_by(key="analysis_output_mode").first()
        analysis_output_mode = output_mode_cfg.value if output_mode_cfg and output_mode_cfg.value else "content"

        success, error_detail = forward_email(
            smtp_host=smtp_cfg["host"],
            smtp_port=smtp_cfg["port"],
            smtp_username=smtp_cfg["username"],
            smtp_password_encrypted=smtp_cfg["password_encrypted"],
            from_email=smtp_cfg["username"],
            to_email=forward_target,
            to_name=forward_name or "",
            original_subject=eml.subject,
            original_body=eml.body_text,
            analysis_result=analysis,
            attachment_paths=full_attachment_paths,
            analysis_output_mode=analysis_output_mode,
        )
        if success:
            log.status = "forwarded"
            log.error_message = None
        else:
            log.status = "failed"
            smtp_info = f"SMTP: {smtp_cfg.get('host', '?')}:{smtp_cfg.get('port', '?')} → {forward_target}"
            log.error_message = f"{smtp_info} | 原因: {error_detail}"
    else:
        log.status = "failed"
        log.error_message = "未配置 SMTP 服务器"

    db.commit()


def _run_llm_analysis(llm_cfg, eml, log, attachment_texts: str, unocr_images: list,
                      account_rules: list, global_rules: list,
                      retry_interval: int, max_retries: int) -> tuple[dict | None, bool]:
    """执行 LLM 分析（含重试），返回 (analysis, llm_failed)"""
    from app.email_fetcher import _run_async_safe
    from app.llm_analyzer import analyze_email

    if not llm_cfg:
        return None, False

    _update_progress(step="analyzing", step_label="正在 LLM 分析...")
    all_rules = account_rules + global_rules
    routing_doc_types = list(dict.fromkeys(
        rule.doc_type for rule in all_rules if rule.doc_type
    ))

    last_error = ""
    for attempt in range(max_retries + 1):
        if attempt > 0:
            logger.info(f"LLM 分析重试 {attempt}/{max_retries} 次（等待 {retry_interval} 秒后）")
            _update_progress(step="analyzing", step_label=f"LLM 重试 {attempt}/{max_retries}...")
            time.sleep(retry_interval)

        try:
            analysis = _run_async_safe(
                analyze_email(
                    api_url=llm_cfg.api_url,
                    api_key_encrypted=llm_cfg.api_key_encrypted,
                    model_name=llm_cfg.model_name,
                    subject=eml.subject,
                    sender=eml.sender,
                    body=eml.body_text,
                    custom_prompt=llm_cfg.analysis_prompt,
                    max_tokens=llm_cfg.max_tokens,
                    temperature=llm_cfg.temperature,
                    attachment_texts=attachment_texts,
                    routing_doc_types=routing_doc_types,
                    images=unocr_images if unocr_images else None,
                    model_type=llm_cfg.model_type or "unknown",
                )
            )

            log.llm_raw_response = json.dumps(analysis, ensure_ascii=False)
            log.doc_type = analysis.get("doc_type")
            log.case_summary = analysis.get("case_summary")
            log.ai_interpretation = analysis.get("ai_interpretation")
            log.urgency = analysis.get("urgency")
            log.key_date = str(analysis.get("key_date")) if analysis.get("key_date") else None
            log.case_number = analysis.get("case_number")
            log.involved_parties = str(analysis.get("involved_parties", ""))[:500] if analysis.get("involved_parties") else None
            log.status = "analyzed"

            if attempt > 0:
                logger.info(f"LLM 分析重试成功（第 {attempt} 次）")
            return analysis, False

        except Exception as e:
            last_error = f"[{type(e).__name__}] {str(e)[:200]}"
            logger.error(f"LLM 分析失败(第{attempt + 1}次): {last_error}")
            if attempt == max_retries:
                logger.error(f"LLM 分析在 {max_retries + 1} 次尝试后全部失败: {last_error}")
                log.status = "failed"
                log.error_message = f"LLM分析失败(已重试{max_retries}次): {last_error}"
                fallback = {
                    "doc_type": "其他法律文书",
                    "case_summary": "大模型分析失败",
                    "ai_interpretation": f"⚠️ 大模型分析失败（已重试 {max_retries} 次），请人工审核。\n\n最后错误：{last_error}",
                    "urgency": "medium", "key_date": "", "case_number": "",
                    "target_lawyer_type": "默认", "involved_parties": "",
                    "confidence": 0.5, "llm_failed": True,
                }
                log.llm_raw_response = json.dumps(fallback, ensure_ascii=False)
                log.doc_type = fallback["doc_type"]
                log.case_summary = fallback["case_summary"]
                log.ai_interpretation = fallback["ai_interpretation"]
                log.urgency = fallback["urgency"]
                return fallback, True

    return None, True  # unreachable


def _resolve_forward_target(matched_rule, analysis, llm_failed: bool, account, db, log) -> tuple:
    """决定转发目标：返回 (smtp_cfg, forward_target, forward_name)

    返回 (None, None, None) 表示跳过或失败（log 已 commit）
    """
    from app.models import DefaultConfig
    from app.mail_forwarder import get_default_smtp_config

    if matched_rule:
        log.target_email = matched_rule.target_email
        smtp_cfg = _infer_smtp_from_account(account)
        if not smtp_cfg and matched_rule.smtp_host and matched_rule.smtp_password_encrypted:
            smtp_cfg = {
                "host": matched_rule.smtp_host,
                "port": matched_rule.smtp_port,
                "username": matched_rule.smtp_username or account.username,
                "password_encrypted": matched_rule.smtp_password_encrypted,
            }
        return smtp_cfg, matched_rule.target_email, matched_rule.target_name

    # 无匹配规则 → 垃圾过滤或默认转发
    llm_doc_type = analysis.get("doc_type", "") if analysis else ""
    llm_confidence = analysis.get("confidence", 0.5) if analysis else 0.0

    is_junk = (
        not llm_failed
        and analysis
        and llm_confidence < 0.3
        and llm_doc_type in ("其他法律文书", "非法律文书")
    )
    if is_junk:
        log.status = "skipped"
        log.error_message = "LLM 判定为非法律文书（低置信度）"
        db.commit()
        return None, None, None

    default_email = db.query(DefaultConfig).filter_by(key="default_forward_email").first()
    if default_email and default_email.value:
        log.target_email = default_email.value
        return get_default_smtp_config(db), default_email.value, "默认收件人"

    log.status = "failed"
    log.error_message = "无匹配路由规则且未配置默认转发邮箱"
    db.commit()
    return None, None, None


def _load_doc_template(db, doc_type: str, account_id: int | None = None) -> str | None:
    """
    加载指定文书类型的默认模板。

    匹配策略：账户专属(exact) → 账户专属(fuzzy) → 全局(exact) → 全局(fuzzy)
    返回模板全文或 None。
    """
    from app.models import DocTemplate

    if not doc_type:
        return None

    # ── 第一层：账户专属精确匹配 ──
    if account_id:
        tmpl = db.query(DocTemplate).filter_by(
            account_id=account_id, doc_type=doc_type, is_default=True
        ).first()
        if tmpl:
            logger.debug(f"加载模板(账户): {tmpl.name} ({doc_type})")
            return tmpl.content

        # 账户专属模糊匹配
        tmpl = db.query(DocTemplate).filter(
            DocTemplate.account_id == account_id,
            DocTemplate.doc_type.like(f"%{doc_type}%"),
            DocTemplate.is_default.is_(True),
        ).first()
        if tmpl:
            logger.debug(f"加载模板(账户模糊): {tmpl.name} ({tmpl.doc_type} ≈ {doc_type})")
            return tmpl.content

    # ── 第二层：全局精确匹配 ──
    tmpl = db.query(DocTemplate).filter_by(
        account_id=None, doc_type=doc_type, is_default=True
    ).first()
    if tmpl:
        logger.debug(f"加载模板(全局): {tmpl.name} ({doc_type})")
        return tmpl.content

    # 全局模糊匹配
    tmpl = db.query(DocTemplate).filter(
        DocTemplate.account_id.is_(None),
        DocTemplate.doc_type.like(f"%{doc_type}%"),
        DocTemplate.is_default.is_(True),
    ).first()
    if tmpl:
        logger.debug(f"加载模板(全局模糊): {tmpl.name} ({tmpl.doc_type} ≈ {doc_type})")
        return tmpl.content

    return None


def _infer_smtp_from_account(account) -> Optional[dict]:
    """从 IMAP 账户推断同域 SMTP 配置（兜底方案）"""
    host = account.imap_host.lower()

    # 已知服务商映射: imap_host → (smtp_host, smtp_port, use_ssl)
    known_providers = {
        "163.com": ("smtp.163.com", 465),
        "126.com": ("smtp.126.com", 465),
        "yeah.net": ("smtp.yeah.net", 465),
        "qq.com": ("smtp.qq.com", 465),
        "foxmail.com": ("smtp.qq.com", 465),
        "gmail.com": ("smtp.gmail.com", 587),
        "outlook.com": ("smtp.office365.com", 587),
        "hotmail.com": ("smtp.office365.com", 587),
        "office365.com": ("smtp.office365.com", 587),
        "aliyun.com": ("smtp.aliyun.com", 465),
    }

    for domain, (smtp_host, smtp_port) in known_providers.items():
        if domain in host:
            logger.info(f"推断 SMTP: {smtp_host}:{smtp_port} (来自 {host})")
            return {
                "host": smtp_host,
                "port": smtp_port,
                "username": account.username,
                "password_encrypted": account.password_encrypted,
            }

    # 通用推测: imap.xxx.com → smtp.xxx.com
    if host.startswith("imap."):
        smtp_host = "smtp." + host[5:]
        logger.info(f"推断 SMTP: {smtp_host}:587")
        return {
            "host": smtp_host,
            "port": 587,
            "username": account.username,
            "password_encrypted": account.password_encrypted,
        }

    return None


def start_scheduler():
    """启动调度器"""
    if not scheduler.running:
        scheduler.start()
        logger.info("调度器已启动")


def shutdown_scheduler():
    """关闭调度器"""
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("调度器已关闭")


# ========== 附件自动清理 ==========

def cleanup_old_attachments():
    """
    根据 log_retention_days 设置自动清理过期附件和日志。
    作为每日定时任务运行，凌晨执行。
    """
    from app.database import SessionLocal
    from app.models import EmailLog, Attachment, DefaultConfig
    from app.config import ATTACHMENTS_DIR

    db = SessionLocal()
    try:
        # 读取保留天数设置：默认 90 天；0 = 永久保留
        cfg = db.query(DefaultConfig).filter_by(key="log_retention_days").first()
        retention_days = int(cfg.value) if cfg and cfg.value and int(cfg.value) > 0 else 90
        # 如果用户显式设置为 0，则永久保留
        if cfg and cfg.value and int(cfg.value) == 0:
            logger.debug("日志保留天数=0，永久保留，跳过自动清理")
            return

        cutoff = datetime.now() - timedelta(days=retention_days)
        old_logs = db.query(EmailLog).filter(EmailLog.created_at < cutoff).all()

        if not old_logs:
            logger.debug(f"无过期日志（截止 {cutoff.strftime('%Y-%m-%d')}）")
            return

        deleted_files = 0
        deleted_logs = 0

        for log_entry in old_logs:
            # 先收集附件文件路径
            attachment_paths_to_delete = []
            for att in log_entry.attachments:
                if att.file_path:
                    full_path = ATTACHMENTS_DIR.parent / att.file_path
                    attachment_paths_to_delete.append(full_path)

            # 先删除数据库记录（包含附件记录 + 日志）
            db.query(Attachment).filter_by(log_id=log_entry.id).delete()
            db.delete(log_entry)
            deleted_logs += 1

            # 数据库提交后再删物理文件，防止失败回滚后文件丢失
            for full_path in attachment_paths_to_delete:
                if full_path.exists():
                    try:
                        os.remove(full_path)
                        deleted_files += 1
                    except OSError as e:
                        logger.warning(f"删除附件文件失败: {full_path} — {e}")

        db.commit()

        # 清理空的子目录（双层: account_name/YYYY-MM-DD）
        for account_dir in ATTACHMENTS_DIR.iterdir():
            if not account_dir.is_dir():
                continue
            # 清理空的日期目录
            for date_dir in account_dir.iterdir():
                if date_dir.is_dir():
                    try:
                        if not any(date_dir.iterdir()):
                            date_dir.rmdir()
                    except OSError:
                        pass
            # 清理空的账户目录
            try:
                if not any(account_dir.iterdir()):
                    account_dir.rmdir()
            except OSError:
                pass

        logger.info(
            f"自动清理完成：删除 {deleted_logs} 条记录，{deleted_files} 个附件文件 "
            f"（保留 {retention_days} 天，截止 {cutoff.strftime('%Y-%m-%d')}）"
        )

    except Exception as e:
        logger.error(f"自动清理出错: {e}", exc_info=True)
        db.rollback()
    finally:
        db.close()


def schedule_cleanup_job():
    """注册每日清理任务（凌晨 3:00）"""
    job_id = "attachment_cleanup"
    if scheduler.get_job(job_id):
        return  # 已注册
    scheduler.add_job(
        func=cleanup_old_attachments,
        trigger=CronTrigger(hour=3, minute=0),
        id=job_id,
        name="附件自动清理",
        replace_existing=True,
    )
    logger.info("附件自动清理任务已注册（每日 03:00）")


# ── 每日运行报告 ──

def _format_uptime(started_at) -> str:
    """格式化运行时长"""
    if not started_at:
        return "未知"
    delta = datetime.now() - datetime.fromisoformat(started_at)
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    mins = remainder // 60
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if mins or not parts:
        parts.append(f"{mins}分钟")
    return "".join(parts)


def _get_smtp_config_for_report(db) -> Optional[dict]:
    """获取用于发送日报的 SMTP 配置（优先级：系统默认SMTP → 第一个邮箱账户推断）"""
    from app.mail_forwarder import get_default_smtp_config
    from app.models import EmailAccount

    # 优先使用系统默认 SMTP
    smtp = get_default_smtp_config(db)
    if smtp:
        return smtp

    # 降级：使用第一个启用邮箱推断 SMTP
    account = db.query(EmailAccount).filter_by(enabled=True).first()
    if not account:
        return None

    return _infer_smtp_from_account(account)


def _read_setting(db, key: str, default: str = "") -> str:
    """从 default_config 表读取单个设置"""
    from app.models import DefaultConfig
    cfg = db.query(DefaultConfig).filter_by(key=key).first()
    return cfg.value if cfg and cfg.value else default


def send_daily_report():
    """生成并发送每日运行报告邮件"""
    from app.database import SessionLocal
    from app.models import EmailAccount, LLMConfig, EmailLog, DefaultConfig

    db = SessionLocal()
    try:
        # 读取日报配置
        enabled_cfg = db.query(DefaultConfig).filter_by(key="daily_report_enabled").first()
        if not enabled_cfg or enabled_cfg.value != "true":
            logger.info("每日报告功能未启用，跳过")
            return

        admin_email = db.query(DefaultConfig).filter_by(key="admin_email").first()
        if not admin_email or not admin_email.value.strip():
            logger.info("未配置管理员邮箱，跳过日报发送")
            return

        to_email = admin_email.value.strip()

        # 获取 SMTP 配置
        smtp_cfg = _get_smtp_config_for_report(db)
        if not smtp_cfg:
            logger.warning("无法获取 SMTP 配置，跳过日报发送")
            return

        from_email = smtp_cfg["username"]

        # ── 统计数据 ──
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        # 今日统计
        today_processed = db.query(func.count(EmailLog.id)).filter(
            EmailLog.created_at >= today
        ).scalar()

        today_forwarded = db.query(func.count(EmailLog.id)).filter(
            EmailLog.created_at >= today, EmailLog.status == "forwarded"
        ).scalar()

        today_failed = db.query(func.count(EmailLog.id)).filter(
            EmailLog.created_at >= today, EmailLog.status == "failed"
        ).scalar()

        today_skipped = db.query(func.count(EmailLog.id)).filter(
            EmailLog.created_at >= today, EmailLog.status == "skipped"
        ).scalar()

        # 全部累计
        total_all = db.query(func.count(EmailLog.id)).scalar()
        forwarded_all = db.query(func.count(EmailLog.id)).filter(
            EmailLog.status == "forwarded"
        ).scalar()
        failed_all = db.query(func.count(EmailLog.id)).filter(
            EmailLog.status == "failed"
        ).scalar()

        # 紧急邮件
        today_urgent = db.query(func.count(EmailLog.id)).filter(
            EmailLog.created_at >= today, EmailLog.urgency == "high"
        ).scalar()

        # 今日 LLM 分析统计
        today_analyzed = db.query(func.count(EmailLog.id)).filter(
            EmailLog.created_at >= today, EmailLog.doc_type.isnot(None), EmailLog.doc_type != ""
        ).scalar()

        # 邮箱账户状态
        accounts = db.query(EmailAccount).filter_by(enabled=True).all()
        account_count = len(accounts)
        account_list = []
        for acc in accounts:
            last_check = db.query(EmailLog.created_at).filter(
                EmailLog.account_id == acc.id
            ).order_by(EmailLog.created_at.desc()).first()
            last_check_str = last_check[0].strftime("%m-%d %H:%M") if last_check and last_check[0] else "暂无记录"
            acc_today = db.query(func.count(EmailLog.id)).filter(
                EmailLog.account_id == acc.id, EmailLog.created_at >= today
            ).scalar()
            account_list.append({
                "name": acc.name,
                "email": acc.username,
                "today_count": acc_today,
                "last_check": last_check_str,
                "interval": acc.check_interval,
            })

        # LLM 状态
        llm_cfg = db.query(LLMConfig).filter_by(is_active=True).first()
        llm_status = f"{llm_cfg.model_name} ({llm_cfg.name})" if llm_cfg else "未启用"
        llm_type = llm_cfg.model_type if llm_cfg else "N/A"

        # 最近 5 条错误
        recent_errors = db.query(EmailLog).filter(
            EmailLog.status == "failed"
        ).order_by(EmailLog.created_at.desc()).limit(5).all()

        # 调度器状态
        scheduler_status = "运行中" if scheduler.running else "已停止"

        # 系统名称
        sys_name_cfg = db.query(DefaultConfig).filter_by(key="system_name").first()
        sys_name = sys_name_cfg.value if sys_name_cfg else "文书分发系统"

        now_str = datetime.now().strftime("%Y年%m月%d日 %H:%M")

        # ── 构建邮件正文 ──
        body = f"""您好，

以下是 {sys_name} 的每日运行报告（{now_str}）：

━━━━━━━━━━━━━━━━━━━━━━━
📊 今日处理概况
━━━━━━━━━━━━━━━━━━━━━━━
  处理总数：{today_processed} 封
  成功转发：{today_forwarded} 封
  LLM 分析：{today_analyzed} 封
  跳过邮件：{today_skipped} 封
  处理失败：{today_failed} 封
  紧急邮件：{today_urgent} 封

━━━━━━━━━━━━━━━━━━━━━━━
📈 累计统计
━━━━━━━━━━━━━━━━━━━━━━━
  累计处理：{total_all} 封
  成功转发：{forwarded_all} 封
  失败合计：{failed_all} 封
  成功率：  {(forwarded_all / total_all * 100):.1f}%{" | 今日: " + f"{(today_forwarded / today_processed * 100):.1f}%" if today_processed > 0 else ""}

━━━━━━━━━━━━━━━━━━━━━━━
📮 监控邮箱（{account_count} 个）
━━━━━━━━━━━━━━━━━━━━━━━
"""

        for acc in account_list:
            body += f"  {acc['name']} ({acc['email']})\n"
            body += f"    今日处理: {acc['today_count']} 封 | 最近检查: {acc['last_check']} | 间隔: {acc['interval']}分钟\n"

        body += f"""
━━━━━━━━━━━━━━━━━━━━━━━
⚙️ 系统状态
━━━━━━━━━━━━━━━━━━━━━━━
  调度引擎：{scheduler_status}
  LLM 模型：{llm_status}
  模型类型：{llm_type}
  服务端口：{_read_setting(db, 'system_port', '8888')}
  监控范围：{_read_setting(db, 'monitor_days', '7')} 天
  日志保留：{'永久' if _read_setting(db, 'log_retention_days', '0') == '0' else _read_setting(db, 'log_retention_days', '0') + ' 天'}
"""

        if recent_errors:
            body += f"""
━━━━━━━━━━━━━━━━━━━━━━━
⚠️ 最近 {len(recent_errors)} 条错误
━━━━━━━━━━━━━━━━━━━━━━━
"""
            for err in recent_errors:
                body += f"  [{err.created_at.strftime('%m-%d %H:%M')}] {err.subject or '(无主题)'}\n"
                body += f"    原因: {(err.error_message or '未知错误')[:120]}\n"

        body += """
━━━━━━━━━━━━━━━━━━━━━━━

此为自动生成的每日报告，请勿回复。
如需修改接收邮箱或发送时间，请前往系统设置页面配置。
"""

        # ── 直接 SMTP 发送（不经过 forward_email 避免 AI 模板包裹） ──
        from app.config import decrypt

        smtp_password = decrypt(smtp_cfg["password_encrypted"])
        report_subject = f"{sys_name} 运行报告 {datetime.now().strftime('%Y-%m-%d')}"

        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = from_email
        msg["To"] = to_email
        msg["Subject"] = report_subject
        msg["X-Forwarded-By"] = "文书分发系统"

        server = None
        try:
            if smtp_cfg["port"] == 465:
                server = smtplib.SMTP_SSL(smtp_cfg["host"], smtp_cfg["port"], timeout=30)
            else:
                server = smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"], timeout=30)
                server.starttls()

            server.login(smtp_cfg["username"], smtp_password)
            server.sendmail(from_email, [to_email], msg.as_string())
            logger.info(f"每日报告已发送至 {to_email}")
        except Exception as e:
            logger.error(f"每日报告发送失败: {e}", exc_info=True)
        finally:
            if server:
                try:
                    server.quit()
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"生成每日报告时出错: {e}", exc_info=True)
    finally:
        db.close()


def schedule_daily_report_job():
    """注册每日报告任务（根据配置时间）"""
    from app.database import SessionLocal
    from app.models import DefaultConfig

    job_id = "daily_report"
    # 先移除旧任务（如有）
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    db = SessionLocal()
    try:
        time_cfg = db.query(DefaultConfig).filter_by(key="daily_report_time").first()
        report_time = time_cfg.value.strip() if time_cfg and time_cfg.value.strip() else "09:00"
    finally:
        db.close()

    try:
        hour, minute = map(int, report_time.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 9, 0

    scheduler.add_job(
        func=send_daily_report,
        trigger=CronTrigger(hour=hour, minute=minute),
        id=job_id,
        name="每日运行报告",
        replace_existing=True,
        misfire_grace_time=900,  # 15分钟容错
    )
    logger.info(f"每日报告任务已注册（每日 {hour:02d}:{minute:02d}）")
