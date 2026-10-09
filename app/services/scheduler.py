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
import unicodedata
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

# 文书类型能力（修订 / 审查 / 合同）统一由 分析提示词/<类型>.md 的文件头部声明，
# 文书类型清单也以该目录下的文件种类为准，代码中不维护任何文书类型清单 ——
# 见 app/services/llm_analyzer.py 的
# should_generate_revision / should_generate_review / is_contract_type。


def get_progress() -> dict:
    """获取当前执行进度（线程安全）"""
    with _progress_lock:
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
    """移除邮件检查任务，同时清理账户锁（防内存泄漏）"""
    job_id = _make_job_id(account_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info(f"已移除检查任务: {job_id}")
    with _account_locks_lock:
        _account_locks.pop(account_id, None)


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
                # 清理提前提交产生的 pending 孤立记录
                # 如果 _process_one_email 已执行早期 commit（初始 EmailLog+Attachment 已持久化），
                # 则 status="pending" 的记录不会被回滚，需手动清理以避免该邮件永不被重试。
                if hasattr(eml, 'message_id') and eml.message_id:
                    try:
                        del_count = email_db.query(EmailLog).filter(
                            EmailLog.message_id == eml.message_id,
                            EmailLog.status == "pending",
                        ).delete(synchronize_session=False)
                        email_db.commit()
                        if del_count:
                            logger.info(f"已清理 pending 孤立记录: {eml.message_id}")
                    except Exception:
                        email_db.rollback()
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

        llm_cfg = _get_role_llm_cfg(db, "analyzer")
        if not llm_cfg:
            logger.warning("没有激活的 LLM 分析配置，将仅靠关键词匹配路由规则")

        # 类型识别模型（第一阶段）；未配置时回退使用文书解读模型
        classifier_row = _get_role_llm_cfg(db, "classifier")
        classifier_cfg = None
        if classifier_row:
            c_prompt = classifier_row.analysis_prompt or ""
            # 若类型识别回退到与文书解读同一模型，第一阶段使用默认分类模板（避免误用分析模板）
            if llm_cfg and classifier_row.id == llm_cfg.id:
                c_prompt = ""
            classifier_cfg = {
                "api_url": classifier_row.api_url,
                "api_key_encrypted": classifier_row.api_key_encrypted,
                "model_name": classifier_row.model_name,
                "analysis_prompt": c_prompt,
                "max_tokens": classifier_row.max_tokens,
            }

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
            "classifier_cfg": classifier_cfg,
            "ocr_cfg": _ocr_cfg,
            "monitor_days": int(_read_setting("monitor_days", "7")),
            "llm_retry_interval": int(_read_setting("llm_retry_interval", "10")),
            "llm_max_retries": int(_read_setting("llm_max_retries", "3")),
            "revision_enabled": _read_setting("revision_enabled", "false") == "true",
            "revision_highlight": _read_setting("revision_highlight", "true") == "true",
            "context_window_tokens": _read_setting("context_window_tokens", "0"),
            "review_template_enabled": _read_setting("review_template_enabled", "false") == "true",
            "review_template_path": _read_setting("review_template_path", "templates/合同审核意见模板.docx"),
            "review_template_path_civil": _read_setting("review_template_path_civil", "templates/律师审核意见模板.docx"),
            "llm_timeout": int(_read_setting("llm_timeout", "180")),
            # ── 多附件分组分析 ──
            "attachment_grouping": _read_setting("attachment_grouping", "false") == "true",
            # ── 全局发件人黑名单 ──
            "global_sender_blacklist": _read_setting("global_sender_blacklist", ""),
        }
    finally:
        db.close()


# 附件分组请求的输出预算下限（推理型模型推理过程计入 max_tokens，预算过小会截断 JSON）
_GROUP_MIN_MAX_TOKENS = 512

# 模型角色 → DefaultConfig 键（角色在 LLM 配置页分配）
_LLM_ROLE_KEYS = {
    "group": "llm_role_group",           # 分组模型（多文书分组）
    "classifier": "llm_role_classifier",  # 类型识别模型（第一阶段）
    "analyzer": "llm_role_analyzer",      # 文书解读审核模型（第二阶段）
}


def _get_role_llm_cfg(db, role: str = "analyzer"):
    """按角色获取 LLM 配置。

    role: group=分组 / classifier=类型识别 / analyzer=文书解读审核。
    角色在 LLM 配置页分配（DefaultConfig 存模型 ID）。未配置时自动回退：
      classifier → analyzer；group → analyzer；analyzer → 第一个激活配置。
    兼容旧 config_role 字段：角色未配置时按旧字段推断（classifier/analyzer）。
    """
    from app.models import LLMConfig, DefaultConfig

    key = _LLM_ROLE_KEYS.get(role)
    if key:
        row = db.query(DefaultConfig).filter_by(key=key).first()
        model_id = (row.value or "").strip() if row else ""
        if model_id.isdigit():
            model = db.query(LLMConfig).filter_by(id=int(model_id)).first()
            if model:
                return model

    # 未配置/无效 → 兼容旧 config_role 或回退
    if role == "classifier":
        legacy = db.query(LLMConfig).filter(
            LLMConfig.is_active == True,  # noqa: E712
            LLMConfig.config_role == "classifier",
        ).order_by(LLMConfig.id).first()
        if legacy:
            return legacy
        return _get_role_llm_cfg(db, "analyzer")
    if role == "group":
        return _get_role_llm_cfg(db, "analyzer")
    # analyzer：兼容旧数据（此前手动激活过的配置优先），否则回退第一个模型
    legacy = db.query(LLMConfig).filter(
        LLMConfig.is_active == True,  # noqa: E712
    ).order_by(LLMConfig.id).first()
    if legacy:
        return legacy
    return db.query(LLMConfig).order_by(LLMConfig.id).first()


def _get_classify_llm_config(ctx: dict, db):
    """获取分组分析用的 LLM 配置（角色：分组模型），返回 LLMConfig 对象或 None"""
    from app.models import LLMConfig, DefaultConfig
    # 分组角色：DefaultConfig llm_role_group；未配置回退文书解读模型
    row = db.query(DefaultConfig).filter_by(key="llm_role_group").first()
    model_id = (row.value or "").strip() if row else ""
    if model_id.isdigit():
        model = db.query(LLMConfig).filter_by(id=int(model_id)).first()
        if model:
            return model
    return _get_role_llm_cfg(db, "analyzer")


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
    # 使用分组模板（自定义模板来自分组模型的 analysis_prompt，未配置时用内置默认）
    from app.services.llm_analyzer import build_group_prompt
    classify_prompt = build_group_prompt(
        subject=eml.subject[:200],
        file_list=file_list,
        custom_prompt=(classify_llm.analysis_prompt or "").strip(),
    )
    try:
        from app.services.email_fetcher import _run_async_safe
        from app.services.llm_analyzer import analyze_email
        from app.config import decrypt
        
        api_key = decrypt(classify_llm.api_key_encrypted) if classify_llm.api_key_encrypted else ""
        api_url = classify_llm.api_url
        
        # 发送轻量分类请求
        result = _run_async_safe(
            _classify_with_llm(api_url, api_key, classify_llm.model_name, classify_prompt,
                               max_tokens=getattr(classify_llm, "max_tokens", 0) or 0)
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


async def _classify_with_llm(api_url: str, api_key: str, model_name: str, prompt: str,
                             max_tokens: int = 0) -> dict | None:
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
        # 与类型识别同理：推理型模型会把推理计入预算，预算过小会截断 JSON，故设下限
        "max_tokens": max(_GROUP_MIN_MAX_TOKENS, int(max_tokens or 0)),
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
    from app.services.email_fetcher import fetch_new_emails
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
    from app.services.email_fetcher import save_attachments, extract_attachment_texts, extract_per_attachment_texts
    from app.services.mail_forwarder import forward_email

    account = ctx["account"]
    llm_cfg = ctx["llm_cfg"]

    # 跳过已被转发的副本（检查 X-Forwarded-By 自定义邮件头）
    from app.services.email_fetcher import is_forwarded_copy
    if is_forwarded_copy(eml.headers):
        logger.info(f"跳过转发副本: {eml.subject}")
        return

    # ── 创建日志记录 ──
    log = EmailLog(
        account_id=account.id,
        message_id=eml.message_id,
        subject=eml.subject,
        sender=eml.sender,
        recipient=eml.recipient,
        received_at=eml.date,
        body_preview=eml.body_text[:500],
        body_text=eml.body_text,
        status="pending",
    )
    db.add(log)
    db.flush()

    # ── 保存附件（原始附件，保持与原邮件附件一致）──
    # 压缩包（zip/rar/7z 等）按原样落盘，不保存解压出的文件；
    # 解压仅用于后续分组与分析链路。
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

    # ── 提前提交，释放写锁 ──
    # 后续 OCR、LLM 等外部调用耗时较长（10-180s），
    # 如果在此期间持写事务，Web 端删除邮件日志将被阻塞至超时。
    db.commit()
    db.refresh(log)


    # ── 展开压缩包附件（zip/rar/7z/tar 等，仅用于分组与分析，不落盘）──
    # 原始压缩包已在上方保存；展开失败不中断主流程（按原附件继续处理）。
    if eml.attachments:
        try:
            from app.services.archive import expand_archive_attachments
            eml.attachments, arc_stats = expand_archive_attachments(eml.attachments)
            if arc_stats.get("expanded"):
                logger.info(
                    f"压缩包附件展开完成: 共展开 {arc_stats.get('extracted', 0)} 个文件"
                    f"（磁盘附件保留原始压缩包，解压文件仅用于分析）"
                )
            for err in arc_stats.get("errors", []):
                logger.warning(f"压缩包附件展开提示: {err}")
        except Exception as e:
            logger.warning(f"压缩包附件展开异常（按原附件继续处理）: {e}")


    # ── 上下文窗口检测（需在附件提取前完成，用于推导附件文本上限） ──
    context_window = 0
    usage_ratio = 0.50
    token_method = "approximate"
    try:
        from app.routes.settings import _get_setting
        from app.services.llm_analyzer import get_effective_context_window, detect_context_window
        from app.config import decrypt

        cw_setting = _get_setting(db, "context_window_tokens") or "0"
        raw_ratio = _get_setting(db, "context_window_usage_ratio") or "0.50"
        raw_method = _get_setting(db, "token_estimation_method") or "approximate"
        usage_ratio = max(0.1, min(0.95, float(raw_ratio)))
        token_method = raw_method if raw_method in ("approximate", "tiktoken") else "approximate"
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

    # ── 由上下文窗口预算协同推导 正文/附件 的文本上限（自动匹配模型窗口） ──
    from app.services.prompt_budget import estimate_tokens, compute_body_and_attachment_char_budget
    from app.services.llm_analyzer import _get_default_prompt
    _template_text = (llm_cfg.analysis_prompt or _get_default_prompt()) if llm_cfg else _get_default_prompt()
    _output_tokens = min(max(getattr(llm_cfg, "max_tokens", 4096) or 4096, 0), 2000) if llm_cfg else 2000
    derived_body, derived_attach = compute_body_and_attachment_char_budget(
        context_window=context_window,
        usage_ratio=usage_ratio,
        output_tokens=_output_tokens,
        template_tokens=estimate_tokens(_template_text),
    )
    # 正文上限：默认由窗口推导；用户显式设置 >0 时作为硬上限（取较小者）。
    # 0 = 自动（由上下文窗口推导）。
    try:
        from app.routes.settings import _get_setting
        _user_body = int(_get_setting(db, "email_body_max_chars") or "0")
    except (ValueError, TypeError):
        _user_body = 0
    body_max_chars = min(derived_body, _user_body) if _user_body > 0 else derived_body
    attachment_max_chars = derived_attach
    logger.info(
        "由窗口推导: 正文上限 %s 字符, 附件上限 %s 字符 (上下文窗口 %s)",
        body_max_chars, attachment_max_chars, context_window,
    )

    # ── 提取附件文本（per-attachment + combined） ──
    all_attachment_texts = ""
    combined_unocr_images = []
    per_att_results = {}
    if eml.attachments:
        # per-attachment 提取（供预分类和分组分析使用）
        per_att_results = extract_per_attachment_texts(
            eml.attachments, ocr_cfg=ctx["ocr_cfg"],
            max_chars=attachment_max_chars,
        )
        # 同时保留 combined 版本（供关键词匹配等使用）
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

    # ── MCP 工具配置（如北大法宝法规检索，仅用于第二阶段文书分析） ──
    mcp_servers = []
    mcp_max_turns = 5
    try:
        from app.routes.settings import _get_setting
        from app.services.mcp_client import parse_server_configs
        if _get_setting(db, "mcp_enabled") == "true":
            raw_cfg = _get_setting(db, "mcp_servers") or ""
            mcp_servers = [s.__dict__ for s in parse_server_configs(raw_cfg)]
            try:
                mcp_max_turns = int(_get_setting(db, "mcp_max_turns") or "5")
            except (ValueError, TypeError):
                mcp_max_turns = 5
            if mcp_servers:
                logger.info("MCP 已启用：%d 个服务器参与第二阶段文书分析", len(mcp_servers))
    except Exception as e:
        logger.warning("读取 MCP 配置失败（本封邮件不使用工具）: [%s] %s", type(e).__name__, str(e)[:150])
        mcp_servers = []

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

    def _cleanup_temp_docx():
        """清理本次生成的临时 docx（修改版文书/审核意见），原始下载附件保留"""
        for _tmp_path in list(revision_paths) + list(review_paths):
            try:
                os.remove(_tmp_path)
            except OSError as _e:
                logger.warning(f"清理临时文书失败: {_tmp_path} — {_e}")

    # 防附件文件名重复计数器
    _seen_revision_names = {}  # base_name → count

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

        # ── 两阶段分析：先类型识别，再按类型分析 ──
        g_analysis, g_failed = _run_llm_analysis(
            llm_cfg=llm_cfg,
            eml=eml,
            log=log,
            attachment_texts=group_attachment_texts,
            unocr_images=group_unocr_images,
            retry_interval=ctx["llm_retry_interval"],
            max_retries=ctx["llm_max_retries"],
            body_max_chars=body_max_chars,
            timeout=ctx["llm_timeout"],
            context_window=context_window,
            usage_ratio=usage_ratio,
            token_method=token_method,
            classifier_cfg=ctx.get("classifier_cfg"),
            mcp_servers=mcp_servers,
            mcp_max_turns=mcp_max_turns,
        )
        all_analyses.append(g_analysis)
        if g_failed:
            all_llm_failed = True

        # 修改版文书生成（逐组）——使用单阶段 LLM 输出的 revised_document
        # 是否可修订由提示词文件头部的 caps: 修订 决定
        from app.services.llm_analyzer import should_generate_revision
        if (ctx.get("revision_enabled")
                and not g_failed
                and g_analysis
                and should_generate_revision(g_analysis.get("doc_type"))
                and llm_cfg):
            try:
                from app.services.mail_forwarder import _generate_revision_docx

                revision_text = g_analysis.get("revised_document")

                if revision_text:
                    # 优先「保持原文格式」：以原始文书为底版注入 Word 原生修订。
                    # 原文书不是可编辑 docx/doc（如 PDF、扫描件）或注入失败时，
                    # 回退为原有的纯文本重建方式，并记录降级日志。
                    _doc_type = g_analysis.get("doc_type", "其他法律文书")
                    _orig_path = None
                    try:
                        _orig_path, _orig_name = _find_original_docx(
                            indices, eml, attachment_records
                        )
                    except Exception as _e:
                        logger.warning(
                            f"查找原始文书失败（{group_label}），回退纯文本重建: "
                            f"[{type(_e).__name__}] {_e}"
                        )

                    rev_path = None
                    if _orig_path:
                        try:
                            from app.services.redline import build_redlined_docx
                            _update_progress(
                                step="analyzing",
                                step_label=f"正在生成修改版文书({group_label})...",
                            )
                            rev_path = build_redlined_docx(
                                original_path=_orig_path,
                                revised_text=revision_text,
                            )
                            if rev_path:
                                logger.info(
                                    f"{group_label} 修改版已保留原文格式"
                                    f"（底版：{_orig_name}）"
                                )
                        except Exception as _e:
                            logger.warning(
                                f"原生修订生成失败（{group_label}），回退纯文本重建: "
                                f"[{type(_e).__name__}] {_e}"
                            )
                            rev_path = None
                    else:
                        logger.info(
                            f"{group_label} 无可编辑的原始文书（docx/doc），"
                            f"修改版将按纯文本重建（格式不保留）"
                        )

                    if not rev_path:
                        rev_path = _generate_revision_docx(
                            revision_text=revision_text,
                            doc_type=_doc_type,
                            original_subject=eml.subject,
                            use_highlight=ctx.get("revision_highlight", True),
                        )
                    if rev_path:
                        revision_paths.append(rev_path)
                        suffix = f"-{g_analysis.get('doc_type', '文书')}" if is_grouping else ""
                        base_name = f"修改版文书{suffix}.docx"
                        if base_name in _seen_revision_names:
                            _seen_revision_names[base_name] += 1
                            ext_dot = base_name.rfind(".")
                            dedup_name = base_name[:ext_dot] + f"_{_seen_revision_names[base_name]}" + base_name[ext_dot:]
                        else:
                            _seen_revision_names[base_name] = 0
                            dedup_name = base_name
                        attachment_records.append({
                            "filename": dedup_name,
                            "file_path": rev_path,
                            "file_size": os.path.getsize(rev_path),
                        })
            except Exception as e:
                logger.error(f"修改版文书生成失败({group_label}): [{type(e).__name__}] {e}")

        # 审查意见模板生成（逐组）
        # 是否出具由提示词文件头部的 caps: 审查 决定；
        # 模板按 #合同 标记分派：合同类用「合同审核意见模板」，其余用「律师审查意见模板」。
        from app.services.llm_analyzer import should_generate_review, is_contract_type
        if (ctx.get("review_template_enabled")
                and not g_failed
                and g_analysis
                and should_generate_review(g_analysis.get("doc_type"))):
            try:
                from app.services.mail_forwarder import _fill_review_template
                from app.config import BASE_DIR

                _is_contract = is_contract_type(g_analysis.get("doc_type"))
                _cfg_key = "review_template_path" if _is_contract else "review_template_path_civil"
                _rel_path = ctx.get(_cfg_key) or ctx.get("review_template_path")
                template_full_path = BASE_DIR / _rel_path
                if not template_full_path.exists():
                    logger.warning(
                        f"{'合同' if _is_contract else '律师'}审查意见模板不存在，跳过生成"
                        f"({group_label}): {_rel_path}"
                    )
                else:
                    _update_progress(step="analyzing", step_label=f"正在生成审查意见({group_label})...")
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
                        rev_base_name = f"审查意见{suffix}.docx"
                        if rev_base_name in _seen_revision_names:
                            _seen_revision_names[rev_base_name] += 1
                            ext_dot = rev_base_name.rfind(".")
                            rev_dedup_name = rev_base_name[:ext_dot] + f"_{_seen_revision_names[rev_base_name]}" + rev_base_name[ext_dot:]
                        else:
                            _seen_revision_names[rev_base_name] = 0
                            rev_dedup_name = rev_base_name
                        attachment_records.append({
                            "filename": rev_dedup_name,
                            "file_path": review_path,
                            "file_size": os.path.getsize(review_path),
                        })
            except Exception as e:
                logger.error(f"审查意见模板生成失败({group_label}): [{type(e).__name__}] {e}")

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
        # 拼接 revision_instructions（合并所有组的结构化修订指令）
        all_instructions = []
        for gi, a in enumerate(all_analyses):
            ri = a.get("revision_instructions", [])
            if ri and isinstance(ri, list) and len(ri) > 0:
                if is_grouping:
                    all_instructions.append({
                        "group_index": gi + 1,
                        "doc_type": a.get("doc_type", "文书"),
                        "instructions": ri,
                    })
                else:
                    all_instructions.extend(ri)
        log.revision_instructions = json.dumps(all_instructions, ensure_ascii=False) if all_instructions else None
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
        analyses=all_analyses,
    )
    if not forward_targets:
        _cleanup_temp_docx()  # 已生成的临时 docx 需清理（否则此提前返回路径会泄漏）
        return  # skipped or failed, already committed

    # ── 执行 SMTP 转发（循环发送到所有目标） ──
    _update_progress(step="forwarding", step_label=f"正在转发到 {len(forward_targets)} 个目标...")
    from app.config import ATTACHMENTS_DIR, resolve_attachment_path
    full_attachment_paths = []
    for att in attachment_records:
        try:
            full_attachment_paths.append(str(resolve_attachment_path(att["file_path"])))
        except ValueError:
            logger.warning(f"附件路径越界，跳过转发: {att.get('file_path')}")
    output_mode_cfg = db.query(DefaultConfig).filter_by(key="analysis_output_mode").first()
    # 兜底值需与 settings.SETTING_DEFAULTS["analysis_output_mode"] 保持一致
    analysis_output_mode = output_mode_cfg.value if output_mode_cfg and output_mode_cfg.value else "content"

    all_success = True
    last_error = ""
    for ft in forward_targets:
        smtp_cfgs = ft["smtp_cfgs"]
        target_email = ft["email"]
        if not smtp_cfgs:
            continue
        # 按候选顺序发送：当前发件服务器不可用时自动回退下一个
        success = False
        error_detail = "无可用 SMTP 候选"
        for idx, smtp_cfg in enumerate(smtp_cfgs):
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
                original_sender=eml.sender,
                original_recipient=eml.recipient,
                original_date=eml.date,
                analyses_results=all_analyses if all_analyses else None,
                attachment_paths=full_attachment_paths,
                analysis_output_mode=analysis_output_mode,
            )
            if success:
                break
            if idx < len(smtp_cfgs) - 1:
                logger.warning(
                    f"发件服务器 {smtp_cfg['host']}:{smtp_cfg['port']} 不可用，"
                    f"自动回退下一个 SMTP → {target_email} | 原因: {error_detail}"
                )
        if not success:
            all_success = False
            hosts = " / ".join(f"{c['host']}:{c['port']}" for c in smtp_cfgs)
            last_error = f"SMTP: {hosts} → {target_email} | 原因: {error_detail}"
            logger.error(f"转发失败: {last_error}")

        # 逐目标记录转发结果（供「按目标邮箱的每日总结邮件」统计）
        _record_forward_result(
            db, log, target_email, ft.get("target_name", ""),
            all_analyses, success, error_detail,
        )

    if all_success:
        log.status = "forwarded"
        log.error_message = None
    else:
        log.status = "failed"
        log.error_message = last_error

    # 清理临时生成的 docx（修改版文书/审核意见），原始下载附件保留
    _cleanup_temp_docx()




def _run_llm_analysis(llm_cfg, eml, log, attachment_texts: str, unocr_images: list,
                      retry_interval: int, max_retries: int,
                      body_max_chars: int = 8000,
                      timeout: int = 180,
                      context_window: int = 0,
                      usage_ratio: float = 0.50,
                      token_method: str = "approximate",
                      classifier_cfg: dict = None,
                      mcp_servers: list = None,
                      mcp_max_turns: int = 5) -> tuple[dict | None, bool]:
    """执行 LLM 分析（含重试），返回 (analysis, llm_failed)"""
    from app.services.email_fetcher import _run_async_safe
    from app.services.llm_analyzer import analyze_email_two_stage

    if not llm_cfg:
        fallback = {
            "doc_type": "其他法律文书",
            "case_summary": "未配置LLM模型，请前往系统设置配置LLM",
            "ai_interpretation": "⚠️ 大模型未配置，无法进行AI分析，请人工审核。",
            "urgency": "medium",
            "key_date": None,
            "case_number": None,
            "involved_parties": "",
            "confidence": 0.5,
            "revised_document": None,
            "llm_failed": True,
        }
        return fallback, True

    _update_progress(step="analyzing", step_label="正在 LLM 分析...")

    last_error = ""
    for attempt in range(max_retries + 1):
        if attempt > 0:
            logger.info(f"LLM 分析重试 {attempt}/{max_retries} 次（等待 {retry_interval} 秒后）")
            _update_progress(step="analyzing", step_label=f"LLM 重试 {attempt}/{max_retries}...")
            time.sleep(retry_interval)

        try:
            analysis = _run_async_safe(
                analyze_email_two_stage(
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
                    body_max_chars=body_max_chars,
                    timeout=timeout,
                    context_window=context_window,
                    usage_ratio=usage_ratio,
                    token_method=token_method,
                    classifier_cfg=classifier_cfg,
                    mcp_servers=mcp_servers,
                    mcp_max_turns=mcp_max_turns,
                    classifier_max_tokens=(classifier_cfg or {}).get("max_tokens") or 0,
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
            # 注意：以上 log.xxx 写操作在组循环中会被 caller 合并覆盖（行 662-698），
            # 此处保留以兼容直接调用 _run_llm_analysis 的非循环场景。

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


def _is_classify_failed(a) -> bool:
    """该组/该次分析的文书类型识别是否失败（返回的是兜底值而非模型判断）"""
    return bool(a) and bool(a.get("classify_failed"))


def _collect_mail_doc_types(analysis, log, analyses=None) -> set:
    """收集本封邮件涉及的全部文书类型（按类型转发规则的匹配依据）。

    优先级：log.doc_types（多文书分组分析时已落库的全部类型）→ analysis["doc_type"]。
    两者都取不到时返回空集合，此时按类型规则一律不命中（不会误转发）。

    analyses 为本次分析的全部分组结果（可选）。多附件分组时 log.doc_types 含
    每一组的结果，其中**类型识别失败**的组返回的是兜底值「其他法律文书」，
    它不是模型判断，不得参与规则匹配（否则会误命中该类型的规则转发到错误邮箱）。
    故此处按分析结果剔除失败组的类型。
    """
    failed_types = {
        str(a.get("doc_type", "") or "").strip()
        for a in (analyses or []) if _is_classify_failed(a)
    }
    names = set()
    raw = getattr(log, "doc_types", None)
    if raw and str(raw).strip():
        names.update(t.strip() for t in str(raw).split(",") if t.strip())
    if analysis:
        single = str(analysis.get("doc_type", "") or "").strip()
        if single:
            names.add(single)
    return {n for n in names if n not in failed_types}


def _match_rule_doc_types(rule, mail_doc_types: set) -> bool:
    """按文书类型规则匹配：规则列出的类型与邮件类型集合有交集即命中。"""
    rule_types = {t.strip() for t in (rule.doc_type or "").split(",") if t.strip()}
    if not rule_types or not mail_doc_types:
        return False
    return bool(rule_types & mail_doc_types)


def _find_original_docx(indices, eml, attachment_records):
    """为某一组附件找出可作为「修订底版」的原始文书路径。

    优先返回该组第一个 .docx / .doc 附件（保留原生格式的最佳候选）；
    没有可编辑文档时返回 (None, "")，调用方回退为纯文本重建。

    返回 (绝对路径, 原始文件名)。
    """
    if not indices:
        return None, ""
    try:
        from app.config import resolve_attachment_path
    except Exception:
        return None, ""

    _DOC_EXT = (".docx", ".doc")
    candidates = []
    for i in indices:
        if i < 0 or i >= len(eml.attachments or []):
            continue
        att = eml.attachments[i]
        fn = getattr(att, "filename", "") or ""
        ext = os.path.splitext(fn)[1].lower()
        if ext not in _DOC_EXT:
            continue
        # .docx 无需外部转换，优先级更高
        rank = 0 if ext == ".docx" else 1
        candidates.append((rank, i, fn))
    if not candidates:
        return None, ""

    candidates.sort(key=lambda x: (x[0], x[1]))
    _, idx, fn = candidates[0]

    rel = None
    if 0 <= idx < len(attachment_records):
        rel = attachment_records[idx].get("file_path")
    if not rel:
        return None, ""

    try:
        full = resolve_attachment_path(rel)
    except Exception as e:
        logger.warning(f"解析原始附件路径失败: {rel} — {type(e).__name__} {e}")
        return None, ""

    if not os.path.exists(str(full)):
        logger.info(f"原始文书不存在，跳过保格式修订: {full}")
        return None, ""
    return str(full), fn


def _record_forward_result(db, log, target_email: str, target_name: str,
                           all_analyses: list, success: bool, error_detail: str = ""):
    """记录单个目标邮箱的转发结果（成功/失败各一条）。

    供「按转发目标邮箱的每日总结邮件」统计使用。
    写入失败不得影响转发主流程，因此整体吞掉异常并记日志。
    """
    from app.models import ForwardRecord

    try:
        doc_types = [a.get("doc_type", "") for a in (all_analyses or []) if a and a.get("doc_type")]
        rec = ForwardRecord(
            log_id=log.id,
            target_email=(target_email or "").strip(),
            target_name=(target_name or "").strip(),
            doc_type=doc_types[0] if doc_types else log.doc_type,
            doc_types=",".join(doc_types) if doc_types else (log.doc_types or ""),
            subject=log.subject,
            success=bool(success),
        )
        db.add(rec)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(
            f"转发记录写入失败（不影响转发）: {target_email} | [{type(e).__name__}] {e}"
        )


def _get_forward_targets(analysis, llm_failed: bool, account, db, log,
                         analyses=None) -> list[dict]:
    """
    决定转发目标列表（两种匹配方式取并集，同一目标邮箱只转发一次）:
    - 垃圾过滤 → 跳过（返回空列表）
    - rule_type=account  → 按监控邮箱匹配（不选账户=全局规则）
    - rule_type=doc_type → 按邮件文书类型匹配（doc_type 与邮件类型集合有交集）

    两种方式均可通过 account_ids 限定生效的监控邮箱范围（空=所有邮箱）。
    全部规则（两种方式）均未命中时兜底默认邮箱；无默认邮箱则标记失败且不转发。

    analyses: 本次分析的全部分组结果（可选）。多附件分组时 analysis 只是其中
    **一组**（按 confidence 选出的），若别的组类型识别失败，其兜底值「其他法律
    文书」会经 log.doc_types 进入类型规则匹配 → 误转发到错误邮箱，故必须按
    全部分组结果判断识别失败并剔除失败组的类型。

    返回 [{"email": str, "smtp_cfgs": [dict, ...]}, ...] 或空列表（跳过）
    """
    from app.models import DefaultConfig, RoutingRule
    from app.services.mail_forwarder import get_default_smtp_config, dedupe_smtp_cfgs

    llm_doc_type = analysis.get("doc_type", "") if analysis else ""
    llm_confidence = analysis.get("confidence", 0.5) if analysis else 0.0

    # ── 类型识别失败：不得据此路由 ──
    # 兜底值「其他法律文书/0.5」与模型真实判断无法区分，若继续走规则匹配，
    # 要么因无规则命中而落到「未配置转发目标」的误导性提示，要么误命中规则转发到错误邮箱。
    # 多附件分组时任一组的识别失败同样不得放行：该组内容未识别，转发出去只会误导律师。
    classify_failed_types = sorted({
        str(a.get("doc_type", "") or "").strip()
        for a in (analyses or []) if _is_classify_failed(a)
    })
    if _is_classify_failed(analysis) or classify_failed_types:
        failed_desc = "、".join(classify_failed_types) or llm_doc_type
        log.status = "failed"
        log.error_message = (
            f"文书类型识别失败（「{failed_desc}」为兜底值，非模型判断），已跳过转发。"
            f"请检查类型识别模型配置（尤其是 max_tokens 是否过小导致 JSON 输出被截断）。"
        )
        return []

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

    # ── 非法律文书 → 也走路由规则（与法律文书同逻辑）──
    # ── 法律文书 → 查路由规则 ──
    mail_doc_types = _collect_mail_doc_types(analysis, log, analyses=analyses)
    targets = []
    seen = set()
    target_names = {}  # email → 律师姓名（同一邮箱多条规则时取首个非空）
    all_rules = db.query(RoutingRule).filter_by(enabled=True).order_by(RoutingRule.id).all()
    for rule in all_rules:
        ids_str = (rule.account_ids or "").strip()
        raw_emails = rule.target_email.strip() if rule.target_email else ""
        if not raw_emails:
            continue

        # 监控邮箱范围（两种匹配方式共用；空 = 不限定）
        if ids_str:
            ids = [x.strip() for x in ids_str.split(",") if x.strip().isdigit()]
            if str(account.id) not in ids:
                continue

        # 匹配方式分派
        if (rule.rule_type or "account") == "doc_type":
            if not _match_rule_doc_types(rule, mail_doc_types):
                continue
        # rule_type=account：邮箱范围已在上方判定通过，直接命中

        # 解析逗号分隔的多个目标邮箱
        emails = [e.strip() for e in raw_emails.split(",") if e.strip()]
        for email in emails:
            if email not in seen:
                targets.append(email)
                seen.add(email)
            _n = (rule.target_name or "").strip()
            if _n and not target_names.get(email):
                target_names[email] = _n

    if not targets:
        default_email = db.query(DefaultConfig).filter_by(key="default_forward_email").first()
        if default_email and default_email.value:
            targets.append(default_email.value.strip())

    if not targets:
        log.status = "failed"
        log.error_message = "未配置转发目标（无匹配路由规则且未配置默认转发邮箱）"
        return []

    log.target_email = ",".join(targets)

    # SMTP 选择优先级（发件服务器）：收件邮箱账户推断(主) → 系统默认(兜底)
    # 发送时按序尝试，收件邮箱 SMTP 不可用时自动回退默认 SMTP（failover）
    smtp_cfgs = []
    account_smtp = _infer_smtp_from_account(account)
    if account_smtp:
        smtp_cfgs.append(account_smtp)
        logger.info(f"主发件服务器: {account_smtp['host']}:{account_smtp['port']}（收件邮箱 {account.username} 推断）")
    default_smtp = get_default_smtp_config(db)
    if default_smtp:
        smtp_cfgs.append(default_smtp)
        logger.info(f"兜底发件服务器: {default_smtp['host']}:{default_smtp['port']}（系统默认 SMTP）")
    smtp_cfgs = dedupe_smtp_cfgs(smtp_cfgs)
    if not smtp_cfgs:
        log.status = "failed"
        log.error_message = "未配置 SMTP 服务器"
        return []

    return [{"email": t, "target_name": target_names.get(t, ""), "smtp_cfgs": smtp_cfgs}
            for t in targets]


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
        scheduler.shutdown(wait=True)
        logger.info("调度器已关闭")


# ========== 附件自动清理 ==========

def cleanup_old_attachments():
    """
    根据 log_retention_days 设置自动清理过期附件和日志。
    作为每日定时任务运行，凌晨执行。
    """
    from app.database import SessionLocal
    from app.models import EmailLog, Attachment, DefaultConfig, ForwardRecord
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
                    try:
                        full_path = resolve_attachment_path(att.file_path)
                    except ValueError:
                        logger.warning(f"附件路径越界，跳过清理: {att.file_path}")
                        continue
                    attachment_paths_to_delete.append(full_path)

            # 先删除数据库记录（子表必须先删，否则外键约束会报错）
            db.query(ForwardRecord).filter_by(log_id=log_entry.id).delete()
            db.query(Attachment).filter_by(log_id=log_entry.id).delete()
            db.delete(log_entry)
            deleted_logs += 1

            # 数据库提交后再删物理文件，防止失败回滚后文件丢失
            db.commit()
            for full_path in attachment_paths_to_delete:
                if full_path.exists():
                    try:
                        os.remove(full_path)
                        deleted_files += 1
                    except OSError as e:
                        logger.warning(f"删除附件文件失败: {full_path} — {e}")

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
    from app.services.mail_forwarder import get_default_smtp_config
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

        # LLM 状态（主分析 LLM）
        llm_cfg = _get_role_llm_cfg(db, "analyzer")
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
        sys_name = sys_name_cfg.value if sys_name_cfg else "邮件智能分析转发系统"

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
        from app.services.email_fetcher import FORWARD_COPY_HEADER, FORWARD_COPY_MARKER
        msg[FORWARD_COPY_HEADER] = FORWARD_COPY_MARKER

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


def _collect_target_summaries(db, since: datetime) -> dict:
    """按转发目标邮箱聚合 [since, now) 期间的转发情况。

    返回 {target_email: {"name": str, "total": int, "types": {doc_type: count},
                        "subjects": [(subject, doc_type)], "failed": int}}

    只统计 success=True 的记录用于「转发了多少封」，失败数单独给出。
    一封邮件的多个类型（多附件分组）按各自类型各计一次，
    因此各类型数量之和可能大于邮件总数 —— 报表中会说明。
    """
    from app.models import ForwardRecord

    records = db.query(ForwardRecord).filter(
        ForwardRecord.created_at >= since
    ).order_by(ForwardRecord.created_at).all()

    result: dict = {}
    for rec in records:
        email = (rec.target_email or "").strip()
        if not email:
            continue
        entry = result.setdefault(email, {
            "name": rec.target_name or "",
            "total": 0,
            "types": {},
            "subjects": [],
            "failed": 0,
        })
        if rec.target_name and not entry["name"]:
            entry["name"] = rec.target_name

        if not rec.success:
            entry["failed"] += 1
            continue

        entry["total"] += 1
        entry["subjects"].append((rec.subject or "(无主题)", rec.doc_type or "未识别"))
        # 多类型邮件：每个类型各计一次（用于类型分布）
        types = [t.strip() for t in (rec.doc_types or rec.doc_type or "").split(",") if t.strip()]
        if not types:
            types = ["未识别"]
        for t in dict.fromkeys(types):  # 去重且保持顺序
            entry["types"][t] = entry["types"].get(t, 0) + 1

    return result


def _send_plain_mail(smtp_cfg: dict, to_email: str, subject: str, body: str) -> bool:
    """用给定 SMTP 配置发送纯文本邮件（不经过 forward_email，避免 AI 模板包裹）"""
    from app.config import decrypt

    server = None
    try:
        smtp_password = decrypt(smtp_cfg["password_encrypted"])
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = smtp_cfg["username"]
        msg["To"] = to_email
        msg["Subject"] = subject
        from app.services.email_fetcher import FORWARD_COPY_HEADER, FORWARD_COPY_MARKER
        msg[FORWARD_COPY_HEADER] = FORWARD_COPY_MARKER

        if smtp_cfg["port"] == 465:
            server = smtplib.SMTP_SSL(smtp_cfg["host"], smtp_cfg["port"], timeout=30)
        else:
            server = smtplib.SMTP(smtp_cfg["host"], smtp_cfg["port"], timeout=30)
            server.starttls()

        server.login(smtp_cfg["username"], smtp_password)
        server.sendmail(smtp_cfg["username"], [to_email], msg.as_string())
        return True
    except Exception as e:
        logger.error(f"邮件发送失败 → {to_email}: [{type(e).__name__}] {e}")
        return False
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass


def _build_target_summary_body(sys_name: str, email: str, name: str,
                               stat: dict, now_str: str, day_str: str) -> str:
    """构建单个转发目标的当日总结正文"""
    total = stat["total"]
    types = stat["types"]
    greeting = f"{name} 您好，" if name else "您好，"

    lines = [
        greeting,
        "",
        f"以下是 {sys_name} 统计的、{day_str} 当天转发给 {email} 的文书汇总（{now_str}）：",
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "📊 今日转发概况",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        f"  转发总数：{total} 封",
    ]
    if stat["failed"]:
        lines.append(f"  转发失败：{stat['failed']} 封（已记录，需人工关注）")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "📁 文书类型分布",
        "━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if types:
        # 按数量降序，数量相同按类型名排序，保证输出稳定
        ordered = sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))

        def _disp_width(s: str) -> int:
            """显示宽度：中日韩全角字符按 2 列计，保证等宽对齐"""
            return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)

        width = max(_disp_width(t) for t, _ in ordered)
        for t, cnt in ordered:
            pad = " " * max(1, width - _disp_width(t) + 2)
            pct = (cnt / total * 100) if total else 0
            lines.append(f"  {t}{pad}{cnt:>3} 封  ({pct:>5.1f}%)")
        if len(ordered) > 1 and sum(types.values()) != total:
            lines.append("")
            lines.append("  说明：一封邮件含多份不同类型文书时会按类型分别计数，")
            lines.append("        因此各类型数量之和可能大于转发总数。")
    else:
        lines.append("  （今日无转发记录）")

    if stat["subjects"]:
        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━",
            "📋 今日转发清单",
            "━━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for subj, dt in stat["subjects"]:
            s = subj if len(subj) <= 60 else subj[:57] + "..."
            lines.append(f"  · [{dt}] {s}")

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "此为自动生成的每日汇总，请勿回复。",
        "如需调整接收邮箱或发送时间，请前往系统设置页面配置。",
    ]
    return "\n".join(lines)


def send_target_summary_reports():
    """按转发目标邮箱逐个发送当日总结邮件。

    每个目标邮箱只收到「转发给他自己」的统计，不含其他目标的邮件。
    数据来自 forward_records 表（逐目标记录，能区分部分成功/失败）。
    """
    from app.database import SessionLocal
    from app.models import DefaultConfig

    db = SessionLocal()
    try:
        if _read_setting(db, "target_summary_enabled", "false") != "true":
            logger.info("转发目标总结邮件未启用，跳过")
            return

        smtp_cfg = _get_smtp_config_for_report(db)
        if not smtp_cfg:
            logger.warning("无法获取 SMTP 配置，跳过转发目标总结邮件")
            return

        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        summaries = _collect_target_summaries(db, today)

        if not summaries:
            logger.info("今日无转发记录，跳过转发目标总结邮件")
            return

        sys_name = _read_setting(db, "system_name", "邮件智能分析转发系统")
        now_str = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        day_str = datetime.now().strftime("%Y年%m月%d日")

        sent = 0
        for email, stat in summaries.items():
            body = _build_target_summary_body(
                sys_name, email, stat["name"], stat, now_str, day_str
            )
            subject = f"{sys_name} 转发汇总 {day_str}（{stat['total']} 封）"
            if _send_plain_mail(smtp_cfg, email, subject, body):
                sent += 1
                logger.info(f"转发目标汇总已发送至 {email}（{stat['total']} 封）")

        logger.info(f"转发目标总结邮件完成：{sent}/{len(summaries)} 个目标发送成功")
    except Exception as e:
        logger.error(f"生成转发目标总结邮件时出错: {e}", exc_info=True)
    finally:
        db.close()


def schedule_daily_report_job():
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


def schedule_target_summary_job():
    """注册「转发目标总结邮件」任务（按配置时间，独立于管理员日报）"""
    from app.database import SessionLocal
    from app.models import DefaultConfig

    job_id = "target_summary"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    db = SessionLocal()
    try:
        time_cfg = db.query(DefaultConfig).filter_by(key="target_summary_time").first()
        report_time = time_cfg.value.strip() if time_cfg and time_cfg.value.strip() else "18:00"
    finally:
        db.close()

    try:
        hour, minute = map(int, report_time.split(":"))
    except (ValueError, AttributeError):
        hour, minute = 18, 0

    scheduler.add_job(
        func=send_target_summary_reports,
        trigger=CronTrigger(hour=hour, minute=minute),
        id=job_id,
        name="转发目标总结邮件",
        replace_existing=True,
        misfire_grace_time=900,  # 15分钟容错
    )
    logger.info(f"转发目标总结任务已注册（每日 {hour:02d}:{minute:02d}）")
