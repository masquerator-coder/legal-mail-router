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

        # ── 1. 加载配置（独立会话，用完即关）──
        ctx = _load_context(account_id)
        if ctx is None:
            return  # account not found

        # ── 2. 拉取邮件 + 批量查重（独立会话，用完即关）──
        new_emails, total = _fetch_and_dedup(ctx["account"], ctx["monitor_days"],
                                                global_blacklist=ctx.get("global_sender_blacklist", ""))
        if not new_emails:
            _update_progress(running=False, step="done", step_label="无新邮件")
            return

        _update_progress(step="processing", step_label="正在处理邮件...", total=total, current=0)

        # ── 3. 逐封处理 — 每封邮件独立数据库事务 ──
        from app.database import SessionLocal

        for idx, eml in enumerate(new_emails, 1):
            _update_progress(current=idx, subject=eml.subject[:60])
            email_db = SessionLocal()
            try:
                _process_one_email(eml, idx, ctx, email_db)
                email_db.commit()          # 成功 → 一次性提交全部变更
            except Exception as e:
                email_db.rollback()        # 失败 → 干净回滚，不影响其他邮件
                logger.error(f"处理邮件 #{idx} ({eml.subject}) 失败: {e}", exc_info=True)
                _update_progress(step="error", step_label=f"邮件{idx}失败: {str(e)[:60]}")
            finally:
                email_db.close()

        _update_progress(step="done", step_label="处理完成", running=False)

    except Exception as e:
        logger.error(f"检查账户 #{account_id} 时发生系统级错误: {e}", exc_info=True)
        _update_progress(step="error", step_label=f"系统错误: {str(e)[:80]}", running=False)
    finally:
        lock.release()


# ── 模块级辅助函数 ──

def _load_context(account_id: int) -> dict | None:
    """加载一次检查所需的所有配置（自包含，内部创建和销毁数据库会话）"""
    from app.database import SessionLocal
    from app.models import EmailAccount, LLMConfig, OCRConfig, DefaultConfig

    db = SessionLocal()
    try:
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
                "pdf_capable": ocr_cfg_row.pdf_capable,
            }

        def _read_setting(key: str, default: str = "") -> str:
            cfg = db.query(DefaultConfig).filter_by(key=key).first()
            return cfg.value if cfg and cfg.value else default

        return {
            "account": account,
            "llm_cfg": llm_cfg,
            "ocr_cfg": _ocr_cfg,
            "monitor_days": int(_read_setting("monitor_days", "7")),
            "llm_retry_interval": int(_read_setting("llm_retry_interval", "10")),
            "llm_max_retries": int(_read_setting("llm_max_retries", "3")),
            "revision_enabled": _read_setting("revision_enabled", "false") == "true",
            "revision_prompt": _read_setting("revision_prompt", ""),
            "revision_highlight": _read_setting("revision_highlight", "true") == "true",
            "context_window_tokens": _read_setting("context_window_tokens", "0"),
            "review_template_enabled": _read_setting("review_template_enabled", "false") == "true",
            "review_template_path": _read_setting("review_template_path", "templates/合同审核意见模板.docx"),
            "llm_timeout": int(_read_setting("llm_timeout", "180")),
            # ── 多附件分组分析 ──
            "attachment_grouping": _read_setting("attachment_grouping", "false") == "true",
            "classify_use_main_llm": _read_setting("classify_use_main_llm", "true") == "true",
            "classify_llm_config_id": _read_setting("classify_llm_config_id", ""),
            # ── 全局发件人黑名单 ──
            "global_sender_blacklist": _read_setting("global_sender_blacklist", ""),
        }
    finally:
        db.close()


def _get_classify_llm_config(ctx: dict, db):
    """获取预分类用的 LLM 配置，返回 LLMConfig 对象或 None"""
    if ctx.get("classify_use_main_llm", True):
        return ctx.get("llm_cfg")
    config_id = ctx.get("classify_llm_config_id", "").strip()
    if config_id and config_id.isdigit():
        from app.models import LLMConfig
        return db.query(LLMConfig).filter_by(id=int(config_id)).first()
    return None


def _classify_attachments(eml, per_att_results: dict, ctx: dict, db) -> list[list[int]]:
    """
    判断邮件附件如何分组。
    
    per_att_results: extract_per_attachment_texts() 的返回值
    
    返回: 附件索引分组列表，如 [[0, 1], [2]]
          → 附件0和1同属一份文书，附件2单独
    """
    n = len(eml.attachments)
    # 附件数 ≤ 1 或 未启用分组 → 所有附件一组
    if n <= 1 or not ctx.get("attachment_grouping"):
        return [list(range(n))]
    
    classify_llm = _get_classify_llm_config(ctx, db)
    if not classify_llm:
        logger.info("分组分析启用但无可用预分类 LLM 配置，按每一附件独立分组")
        return [[i] for i in range(n)]
    
    # 构建分类 prompt（使用文件名 + 文本预览前 150 字）
    entries = []
    for idx in range(n):
        att = eml.attachments[idx]
        info = per_att_results.get(idx, {})
        preview = info.get("text", "")[:150].replace("\n", " ").strip()
        if not preview:
            if info.get("images"):
                preview = "【图片/扫描件，需多模态分析】"
            else:
                preview = "【无法提取文本】"
        entries.append(f"{idx}. {att.filename}\n   内容预览: {preview}")
    
    file_list = "\n".join(entries)
    classify_prompt = f"""你是法律文档分类助手。以下是邮件附件列表及内容预览，请判断这些附件：
A) 属于同一份法律文书的组成部分（如合同正文+附件表格+签章页）→ 归为一组
B) 包含多份独立的不同文书 → 各自成组

邮件主题：{eml.subject[:200]}

附件列表：
{file_list}

请返回 JSON（只返回 JSON，不要多余文字）：
{{"groups": [[indices...], ...]}}

示例1（采购合同+报价单+保密协议）: {{"groups": [[0, 1], [2]]}}
示例2（起诉状+证据清单+证据材料+证据1）: {{"groups": [[0, 1, 2, 3]]}}
示例3（只有1个附件）: {{"groups": [[0]]}}
"""
    try:
        from app.email_fetcher import _run_async_safe
        from app.llm_analyzer import analyze_email
        from app.config import decrypt
        
        api_key = decrypt(classify_llm.api_key_encrypted) if classify_llm.api_key_encrypted else ""
        api_url = classify_llm.api_url
        
        # 发送轻量分类请求
        result = _run_async_safe(
            _classify_with_llm(api_url, api_key, classify_llm.model_name, classify_prompt)
        )
        if result and isinstance(result, dict) and "groups" in result:
            groups = result["groups"]
            # 验证分组合法性
            all_indices = set()
            for g in groups:
                if not isinstance(g, list):
                    raise ValueError(f"分组格式错误: {g}")
                for i in g:
                    if not isinstance(i, int) or i < 0 or i >= n:
                        raise ValueError(f"越界索引: {i}")
                    all_indices.add(i)
            if all_indices == set(range(n)):
                logger.info(f"LLM 预分类完成: {len(groups)} 组 ({groups})")
                return groups
            else:
                logger.warning(f"LLM 分组不完整: 覆盖 {len(all_indices)}/{n} 个附件，回退到独立分组")
        else:
            logger.warning(f"LLM 分类返回格式异常: {result}")
    except Exception as e:
        logger.error(f"LLM 预分类失败: {e}")
    
    # 安全兜底：每个附件独立成组
    return [[i] for i in range(n)]


async def _classify_with_llm(api_url: str, api_key: str, model_name: str, prompt: str) -> dict | None:
    """发送轻量分类请求到 LLM，返回 JSON 结果"""
    import json
    import httpx
    
    if not api_url.endswith("/chat/completions"):
        api_url = api_url.rstrip("/") + "/chat/completions"
    
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "你是一个文档分类助手。严格按照用户要求的 JSON 格式返回结果，不要包含任何多余文字。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 200,
        "temperature": 0,
    }
    
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(api_url, headers=headers, json=payload)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
    
    # 清理可能的 markdown 代码块
    if content.startswith("```"):
        content = content.split("\n", 1)[-1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
    content = content.strip()
    
    return json.loads(content)


def _fetch_and_dedup(account, monitor_days: int, global_blacklist: str = "") -> tuple[list, int]:
    """拉取邮件并批量查重（自包含，内部创建和销毁数据库会话用于查重）"""
    from app.database import SessionLocal
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
        global_blacklist=global_blacklist,
    )
    if not emails:
        return [], 0

    batch_msg_ids = [eml.message_id for eml in emails if eml.message_id]
    existing_ids: set = set()
    if batch_msg_ids:
        dedup_db = SessionLocal()
        try:
            existing_ids = set(
                row[0] for row in dedup_db.query(EmailLog.message_id)
                .filter(EmailLog.message_id.in_(batch_msg_ids))
                .all()
            )
        finally:
            dedup_db.close()
    new_emails = [eml for eml in emails if eml.message_id not in existing_ids]
    return new_emails, len(new_emails)


def _process_one_email(eml, idx: int, ctx: dict, db):
    """处理单封邮件：保存附件 → 分组 → LLM 分析 → 路由匹配 → 转发"""
    from app.models import EmailLog, Attachment, DefaultConfig
    from app.email_fetcher import save_attachments, extract_attachment_texts, extract_per_attachment_texts
    from app.mail_forwarder import forward_email

    account = ctx["account"]
    llm_cfg = ctx["llm_cfg"]

    # 跳过已被转发的副本（检查 X-Forwarded-By 自定义邮件头）
    if eml.headers.get("x-forwarded-by") == "文书分拣系统":
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


    # ── 提取附件文本（per-attachment + combined） ──
    all_attachment_texts = ""
    combined_unocr_images = []
    per_att_results = {}
    if eml.attachments:
        # per-attachment 提取（供预分类和分组分析使用）
        per_att_results = extract_per_attachment_texts(eml.attachments, ocr_cfg=ctx["ocr_cfg"])
        # 同时保留 combined 版本（供关键词匹配和知识库检索用）
        texts = []
        imgs = []
        for idx_a, info in per_att_results.items():
            if info.get("text"):
                texts.append(f"=== 附件: {info['filename']} ===\n{info['text']}")
            imgs.extend(info.get("images", []))
        if texts:
            all_attachment_texts = "\n\n".join(texts)
        combined_unocr_images = imgs
        if all_attachment_texts:
            logger.info(f"已提取 {len(eml.attachments)} 个附件文本 ({len(all_attachment_texts)} 字符)")

    # ── 法律知识库检索（一次，基于邮件正文） ──
    kb_context = ""
    kb_max_chars = 5000
    body_max_chars = 8000
    try:
        from app.routes.settings import get_kb_config, _get_setting
        from app.kb_client import search_and_format
        kb_cfg = get_kb_config(db)
        try:
            kb_max_chars = int(_get_setting(db, "kb_search_max_chars") or "5000")
        except (ValueError, TypeError):
            pass
        try:
            body_max_chars = int(_get_setting(db, "email_body_max_chars") or "8000")
        except (ValueError, TypeError):
            pass
        if kb_cfg["enabled"] and kb_cfg["project_id"]:
            query = f"{eml.subject} {eml.body_text[:500]}"
            _update_progress(step="searching_kb", step_label="正在检索法律知识库...")
            kb_context = search_and_format(
                query=query,
                project_id=kb_cfg["project_id"],
                api_base=kb_cfg["api_base"],
                token=kb_cfg["token"],
                top_k=5,
                max_chars=kb_max_chars,
            )
            if kb_context:
                logger.info(f"知识库检索成功，注入 {len(kb_context)} 字符法律参考")
            else:
                logger.info("知识库检索无结果")
    except Exception as e:
        logger.warning(f"知识库检索异常（不中断主流程）: {e}")

    # ── 上下文窗口检测 ──
    context_window = 0
    usage_ratio = 0.50
    token_method = "approximate"
    try:
        from app.routes.settings import _get_setting
        from app.llm_analyzer import get_effective_context_window, detect_context_window
        from app.config import decrypt

        # 读取 DB 中的上下文窗口设置
        cw_setting = _get_setting(db, "context_window_tokens") or "0"
        raw_ratio = _get_setting(db, "context_window_usage_ratio") or "0.50"
        raw_method = _get_setting(db, "token_estimation_method") or "approximate"

        usage_ratio = max(0.1, min(0.95, float(raw_ratio)))
        token_method = raw_method if raw_method in ("approximate", "tiktoken") else "approximate"

        # 获取有效上下文窗口
        if llm_cfg:
            api_key = decrypt(llm_cfg.api_key_encrypted) if llm_cfg.api_key_encrypted else ""
            context_window = get_effective_context_window(
                cw_setting, llm_cfg.api_url, api_key, llm_cfg.model_name,
            )
            logger.info(
                "LLM 上下文窗口: %s tokens, 输入预算占比: %s, token估算方法: %s",
                context_window, usage_ratio, token_method,
            )
    except Exception as e:
        logger.warning(f"上下文窗口检测异常（使用默认值）: {e}")
        context_window = 0  # 跳过预算检查

    # ── 预分类：确定附件分组 ──
    groups = _classify_attachments(eml, per_att_results, ctx, db)
    if groups == [list(range(len(eml.attachments)))] or len(groups) <= 1:
        is_grouping = False
    else:
        is_grouping = True

    # ── 逐组分析 ──
    all_analyses = []
    all_llm_failed = False
    revision_paths = []
    review_paths = []

    for g_idx, indices in enumerate(groups):
        # 组装该组的附件文本和图片
        group_attachment_texts = ""
        group_unocr_images = []
        if eml.attachments:
            group_texts = []
            for i in indices:
                info = per_att_results.get(i, {})
                if info.get("text"):
                    group_texts.append(f"=== 附件: {info['filename']} ===\n{info['text']}")
                group_unocr_images.extend(info.get("images", []))
            if group_texts:
                group_attachment_texts = "\n\n".join(group_texts)

        group_label = f"第{g_idx + 1}组" if is_grouping else "文书"
        logger.info(f"分析 {group_label}: 附件索引 {indices}")

        # LLM 分析
        g_analysis, g_failed = _run_llm_analysis(
            llm_cfg=llm_cfg,
            eml=eml,
            log=log,
            attachment_texts=group_attachment_texts,
            unocr_images=group_unocr_images,
            retry_interval=ctx["llm_retry_interval"],
            max_retries=ctx["llm_max_retries"],
            kb_context=kb_context,
            body_max_chars=body_max_chars,
            timeout=ctx["llm_timeout"],
            context_window=context_window,
            usage_ratio=usage_ratio,
            token_method=token_method,
        )
        all_analyses.append(g_analysis)
        if g_failed:
            all_llm_failed = True

        # 修改版文书生成（逐组）
        if (ctx.get("revision_enabled")
                and not g_failed
                and g_analysis
                and llm_cfg):
            try:
                from app.llm_analyzer import get_effective_context_window, generate_revision
                from app.config import decrypt
                from app.mail_forwarder import _generate_revision_docx
                from app.email_fetcher import _run_async_safe

                context_window = get_effective_context_window(
                    ctx.get("context_window_tokens", "0"),
                    llm_cfg.api_url,
                    decrypt(llm_cfg.api_key_encrypted) if llm_cfg.api_key_encrypted else "",
                    llm_cfg.model_name,
                )
                original_truncation = int(context_window * 0.45)
                revision_max_tokens = max(1000, min(int(context_window * 0.05), 16000))

                original_text = (group_attachment_texts if group_attachment_texts else eml.body_text)[:original_truncation]
                template = _load_doc_template(db, g_analysis.get("doc_type", ""), ctx["account"].id)

                _update_progress(step="analyzing", step_label=f"正在生成修改版文书({group_label})...")
                revision_text = _run_async_safe(
                    generate_revision(
                        api_url=llm_cfg.api_url,
                        api_key_encrypted=llm_cfg.api_key_encrypted,
                        model_name=llm_cfg.model_name,
                        doc_type=g_analysis.get("doc_type", "其他法律文书"),
                        original_text=original_text,
                        ai_interpretation=g_analysis.get("ai_interpretation", ""),
                        custom_prompt=ctx.get("revision_prompt", ""),
                        template=template,
                        max_tokens=revision_max_tokens,
                        temperature=llm_cfg.temperature,
                        timeout=ctx.get("llm_timeout", 180),
                    )
                )
                if revision_text:
                    rev_path = _generate_revision_docx(
                        revision_text=revision_text,
                        doc_type=g_analysis.get("doc_type", "其他法律文书"),
                        original_subject=eml.subject,
                        use_highlight=ctx.get("revision_highlight", True),
                    )
                    if rev_path:
                        revision_paths.append(rev_path)
                        suffix = f"-{g_analysis.get('doc_type', '文书')}" if is_grouping else ""
                        attachment_records.append({
                            "filename": f"修改版文书{suffix}.docx",
                            "file_path": rev_path,
                            "file_size": os.path.getsize(rev_path),
                        })
            except Exception as e:
                logger.error(f"修改版文书生成失败({group_label}): [{type(e).__name__}] {e}")

        # 审核意见模板生成（逐组）
        if (ctx.get("review_template_enabled")
                and not g_failed
                and g_analysis
                and ctx.get("review_template_path")):
            try:
                from app.mail_forwarder import _fill_review_template
                from app.config import BASE_DIR

                template_full_path = BASE_DIR / ctx["review_template_path"]
                _update_progress(step="analyzing", step_label=f"正在生成审核意见({group_label})...")
                # 收集该组的附件文件名（用于提取文书标题）
                group_filenames = [eml.attachments[i].filename for i in indices] if eml.attachments else []
                review_path = _fill_review_template(
                    template_path=str(template_full_path),
                    analysis=g_analysis,
                    original_subject=eml.subject,
                    sender=eml.sender,
                    body_text=group_attachment_texts or eml.body_text,
                    attachment_filenames=group_filenames,
                )
                if review_path:
                    review_paths.append(review_path)
                    suffix = f"-{g_analysis.get('doc_type', '文书')}" if is_grouping else ""
                    attachment_records.append({
                        "filename": f"审核意见{suffix}.docx",
                        "file_path": review_path,
                        "file_size": os.path.getsize(review_path),
                    })
            except Exception as e:
                logger.error(f"审核意见模板生成失败({group_label}): [{type(e).__name__}] {e}")

    # ── 提交分析结果并提前固化 ──
    # 1B 存储格式：doc_type 存首个，doc_types 存全部，llm_raw_response 存数组
    if all_analyses:
        primary = all_analyses[0]
        log.llm_raw_response = json.dumps(all_analyses, ensure_ascii=False)
        log.doc_type = primary.get("doc_type") if primary else None
        log.doc_types = ",".join(
            a.get("doc_type", "") for a in all_analyses if a and a.get("doc_type")
        )
        # 拼接 ai_interpretation
        interps = []
        for gi, a in enumerate(all_analyses):
            if a and a.get("ai_interpretation"):
                if is_grouping:
                    interps.append(f"【第{gi + 1}组 - {a.get('doc_type', '文书')}】\n{a['ai_interpretation']}")
                else:
                    interps.append(a["ai_interpretation"])
        log.ai_interpretation = "\n\n---\n\n".join(interps) if interps else (primary.get("ai_interpretation", "") if primary else "")
        # 取首个的关键信息
        log.case_summary = primary.get("case_summary") if primary else None
        log.urgency = primary.get("urgency") if primary else None
        log.key_date = str(primary.get("key_date")) if primary and primary.get("key_date") else None
        log.case_number = primary.get("case_number") if primary else None
        log.involved_parties = str(primary.get("involved_parties", ""))[:500] if primary and primary.get("involved_parties") else None
        log.status = "analyzed"


    # 选用于垃圾过滤的 analysis
    route_analysis = None
    route_llm_failed = all_llm_failed
    if all_analyses:
        valid = [a for a in all_analyses if a and not a.get("llm_failed")]
        route_analysis = max(valid, key=lambda a: float(a.get("confidence", 0))) if valid else all_analyses[0]
    if not route_analysis:
        route_analysis = None
        route_llm_failed = False

    # ── 转发决策 ──
    forward_targets = _get_forward_targets(
        analysis=route_analysis,
        llm_failed=route_llm_failed,
        account=account,
        db=db,
        log=log,
    )
    if not forward_targets:
        return  # skipped or failed, already committed

    # ── 执行 SMTP 转发（循环发送到所有目标） ──
    _update_progress(step="forwarding", step_label=f"正在转发到 {len(forward_targets)} 个目标...")
    from app.config import ATTACHMENTS_DIR, resolve_attachment_path
    full_attachment_paths = []
    for att in attachment_records:
        full_attachment_paths.append(str(resolve_attachment_path(att["file_path"])))
    output_mode_cfg = db.query(DefaultConfig).filter_by(key="analysis_output_mode").first()
    analysis_output_mode = output_mode_cfg.value if output_mode_cfg and output_mode_cfg.value else "content"

    all_success = True
    last_error = ""
    for ft in forward_targets:
        smtp_cfg = ft["smtp_cfg"]
        target_email = ft["email"]
        if not smtp_cfg:
            continue
        success, error_detail = forward_email(
            smtp_host=smtp_cfg["host"],
            smtp_port=smtp_cfg["port"],
            smtp_username=smtp_cfg["username"],
            smtp_password_encrypted=smtp_cfg["password_encrypted"],
            from_email=smtp_cfg["username"],
            to_email=target_email,
            to_name="",
            original_subject=eml.subject,
            original_body=eml.body_text,
            analyses_results=all_analyses if all_analyses else None,
            attachment_paths=full_attachment_paths,
            analysis_output_mode=analysis_output_mode,
        )
        if not success:
            all_success = False
            last_error = f"SMTP: {smtp_cfg.get('host', '?')}:{smtp_cfg.get('port', '?')} → {target_email} | 原因: {error_detail}"
            logger.error(f"转发失败: {last_error}")

    if all_success:
        log.status = "forwarded"
        log.error_message = None
    else:
        log.status = "failed"
        log.error_message = last_error




def _run_llm_analysis(llm_cfg, eml, log, attachment_texts: str, unocr_images: list,
                      retry_interval: int, max_retries: int,
                      kb_context: str = "",
                      body_max_chars: int = 8000,
                      timeout: int = 180,
                      context_window: int = 0,
                      usage_ratio: float = 0.50,
                      token_method: str = "approximate") -> tuple[dict | None, bool]:
    """执行 LLM 分析（含重试），返回 (analysis, llm_failed)"""
    from app.email_fetcher import _run_async_safe
    from app.llm_analyzer import analyze_email

    if not llm_cfg:
        return None, False

    _update_progress(step="analyzing", step_label="正在 LLM 分析...")

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
                    images=unocr_images if unocr_images else None,
                    model_type=llm_cfg.model_type or "unknown",
                    kb_context=kb_context,
                    body_max_chars=body_max_chars,
                    timeout=timeout,
                    context_window=context_window,
                    usage_ratio=usage_ratio,
                    token_method=token_method,
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
                    "involved_parties": "",
                    "confidence": 0.5, "llm_failed": True,
                }
                log.llm_raw_response = json.dumps(fallback, ensure_ascii=False)
                log.doc_type = fallback["doc_type"]
                log.case_summary = fallback["case_summary"]
                log.ai_interpretation = fallback["ai_interpretation"]
                log.urgency = fallback["urgency"]
                return fallback, True

    return None, True  # unreachable


def _get_forward_targets(analysis, llm_failed: bool, account, db, log) -> list[dict]:
    """
    决定转发目标列表:
    - 垃圾过滤 → 跳过（返回空列表）
    - 非法律文书 → 转发到默认邮箱
    - 法律文书 → 按路由规则匹配（account_ids 匹配），兜底默认邮箱

    返回 [{"email": str, "smtp_cfg": dict}, ...] 或空列表（跳过）
    """
    from app.models import DefaultConfig, RoutingRule
    from app.mail_forwarder import get_default_smtp_config

    llm_doc_type = analysis.get("doc_type", "") if analysis else ""
    llm_confidence = analysis.get("confidence", 0.5) if analysis else 0.0

    # ── 垃圾过滤 ──
    is_junk = (
        not llm_failed
        and analysis
        and llm_confidence < 0.3
        and llm_doc_type in ("其他法律文书", "非法律文书")
    )
    if is_junk:
        log.status = "skipped"
        log.error_message = "LLM 判定为非法律文书（低置信度）"
        return []

    # ── 非法律文书 → 默认邮箱 ──
    if analysis and llm_doc_type == "非法律文书":
        default_email = db.query(DefaultConfig).filter_by(key="default_forward_email").first()
        if default_email and default_email.value:
            targets = [default_email.value.strip()]
        else:
            log.status = "failed"
            log.error_message = "非法律文书未配置默认转发邮箱"
            return []
        log.target_email = targets[0]
        smtp = _infer_smtp_from_account(account) or get_default_smtp_config(db)
        if not smtp:
            log.status = "failed"
            log.error_message = "未配置 SMTP 服务器"
            return []
        return [{"email": t, "smtp_cfg": smtp} for t in targets]

    # ── 法律文书 → 查路由规则 ──
    targets = []
    seen = set()
    all_rules = db.query(RoutingRule).filter_by(enabled=True).order_by(RoutingRule.id).all()
    for rule in all_rules:
        ids_str = (rule.account_ids or "").strip()
        raw_emails = rule.target_email.strip() if rule.target_email else ""
        if not raw_emails:
            continue
        # 解析逗号分隔的多个目标邮箱
        emails = [e.strip() for e in raw_emails.split(",") if e.strip()]
        for email in emails:
            if email in seen:
                continue
            if ids_str:
                ids = [x.strip() for x in ids_str.split(",") if x.strip().isdigit()]
                if str(account.id) in ids:
                    targets.append(email)
                    seen.add(email)
            else:
                targets.append(email)
                seen.add(email)

    if not targets:
        default_email = db.query(DefaultConfig).filter_by(key="default_forward_email").first()
        if default_email and default_email.value:
            targets.append(default_email.value.strip())

    if not targets:
        log.status = "failed"
        log.error_message = "未配置转发目标（无匹配路由规则且未配置默认转发邮箱）"
        return []

    log.target_email = ",".join(targets)
    smtp = _infer_smtp_from_account(account) or get_default_smtp_config(db)
    if not smtp:
        log.status = "failed"
        log.error_message = "未配置 SMTP 服务器"
        return []

    return [{"email": t, "smtp_cfg": smtp} for t in targets]


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
    """关闭调度器（最多等待 30 秒让正在执行的任务完成，保障数据一致性）"""
    if scheduler.running:
        scheduler.shutdown(wait=True, timeout=30)
        logger.info("调度器已关闭")


# ========== 附件自动清理 ==========

def cleanup_old_attachments():
    """
    根据 log_retention_days 设置自动清理过期附件和日志。
    作为每日定时任务运行，凌晨执行。
    """
    from app.database import SessionLocal
    from app.models import EmailLog, Attachment, DefaultConfig
    from app.config import ATTACHMENTS_DIR, resolve_attachment_path

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
                    full_path = resolve_attachment_path(att.file_path)
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
  成功率：  {f"{(forwarded_all / total_all * 100):.1f}%" if total_all > 0 else "暂无数据"}{" | 今日: " + f"{(today_forwarded / today_processed * 100):.1f}%" if today_processed > 0 else ""}

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
  服务端口：{_read_setting(db, 'system_port', '8020')}
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
