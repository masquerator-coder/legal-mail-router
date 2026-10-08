"""
处理日志路由
"""
import csv
import io
import json
import logging
import zipfile
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Query, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db, db_retry_commit
from app.models import EmailLog, Attachment, EmailAccount, DefaultConfig, ForwardRecord
from app.services.scheduler import scheduler
from app.config import ATTACHMENTS_DIR, resolve_attachment_path
from app.services.mail_forwarder import forward_email, get_default_smtp_config, dedupe_smtp_cfgs
from app.csrf import check_csrf
from urllib.parse import quote

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/logs", tags=["处理日志"])


@router.get("")
async def list_logs(
    request: Request,
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    status: str = Query(""),
    keyword: str = Query(""),
):
    per_page = 20
    query = db.query(EmailLog)

    if status:
        query = query.filter(EmailLog.status == status)
    if keyword:
        query = query.filter(
            (EmailLog.subject.ilike(f"%{keyword}%")) |
            (EmailLog.sender.ilike(f"%{keyword}%")) |
            (EmailLog.case_summary.ilike(f"%{keyword}%"))
        )

    total = query.count()
    total_pages = max(1, (total + per_page - 1) // per_page)

    logs = query.order_by(EmailLog.created_at.desc()).offset(
        (page - 1) * per_page
    ).limit(per_page).all()

    # 预加载每条日志的附件信息
    log_ids = [log.id for log in logs]
    attachment_map = {}
    if log_ids:
        atts = db.query(Attachment).filter(Attachment.log_id.in_(log_ids)).all()
        for att in atts:
            attachment_map.setdefault(att.log_id, []).append(att)

    # 统计
    status_counts = db.query(EmailLog.status, func.count(EmailLog.id)).group_by(EmailLog.status).all()
    counts = {s: c for s, c in status_counts}

    return request.app.state.templates.TemplateResponse(request, "logs.html", {
        "request": request,
        "active_page": "logs",
        "logs": logs,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "status_filter": status,
        "keyword": keyword,
        "status_encoded": quote(status, safe='') if status else '',
        "keyword_encoded": quote(keyword, safe='') if keyword else '',
        "counts": counts,
        "attachment_map": attachment_map,
        "scheduler_running": scheduler.running,
        "status_labels": {
            "pending": "待处理",
            "analyzed": "已分析",
            "forwarded": "已转发",
            "failed": "失败",
            "skipped": "已跳过",
        },
        "urgency_map": {"high": "🔴", "medium": "🟡", "low": "🟢"},
    })


@router.get("/detail/{log_id}")
async def log_detail(log_id: int, request: Request, db: Session = Depends(get_db)):
    log = db.query(EmailLog).filter_by(id=log_id).first()
    if not log:
        return request.app.state.templates.TemplateResponse(request, "logs.html", {
            "request": request, "active_page": "logs",
            "logs": [], "error": "记录不存在",
        })

    import json
    llm_result = None
    group_count = 0
    if log.llm_raw_response:
        try:
            parsed = json.loads(log.llm_raw_response)
            # 多附件分组时存储为 JSON 数组 [analysis_1, analysis_2, ...]
            # 模板按单分析设计，取第一条作为主分析
            if isinstance(parsed, list):
                group_count = len(parsed)
                llm_result = parsed[0] if parsed else None
            else:
                llm_result = parsed
        except Exception:
            llm_result = {"raw": log.llm_raw_response}

    attachments = db.query(Attachment).filter_by(log_id=log.id).all()

    # 解析结构化修订指令
    revision_instructions_parsed = None
    if log.revision_instructions:
        try:
            parsed = json.loads(log.revision_instructions)
            if parsed and (isinstance(parsed, list) and len(parsed) > 0):
                revision_instructions_parsed = parsed
        except Exception:
            pass

    return request.app.state.templates.TemplateResponse(request, "log_detail.html", {
        "request": request,
        "active_page": "logs",
        "log": log,
        "llm_result": llm_result,
        "attachments": attachments,
        "group_count": group_count,
        "revision_instructions": revision_instructions_parsed,
        "scheduler_running": scheduler.running,
        "urgency_map": {"high": "🔴", "medium": "🟡", "low": "🟢"},
    })


def _infer_smtp_for_log(account, db) -> dict | None:
    """从邮箱账户推断 SMTP 配置（同 scheduler._infer_smtp_from_account 逻辑）"""
    host = account.imap_host.lower()
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
            return {
                "host": smtp_host,
                "port": smtp_port,
                "username": account.username,
                "password_encrypted": account.password_encrypted,
            }
    if host.startswith("imap."):
        smtp_host = "smtp." + host[5:]
        return {
            "host": smtp_host,
            "port": 587,
            "username": account.username,
            "password_encrypted": account.password_encrypted,
        }
    return None


@router.post("/{log_id}/resend")
async def resend_email(
    log_id: int,
    request: Request,
    db: Session = Depends(get_db),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """重新转发失败的邮件（不重新 LLM 分析，使用已有分析结果）"""
    check_csrf(request, form_csrf)

    log = db.query(EmailLog).filter_by(id=log_id).first()
    if not log:
        return {"success": False, "message": "记录不存在"}

    if log.status != "failed":
        return {"success": False, "message": "只能重新转发失败状态的邮件"}

    # 获取邮箱账户
    account = db.query(EmailAccount).filter_by(id=log.account_id).first()
    if not account:
        return {"success": False, "message": "关联邮箱账户不存在"}

    # 获取 SMTP 配置候选（优先收件邮箱账户推断，不可用时回退默认 SMTP）
    smtp_cfgs = dedupe_smtp_cfgs([
        _infer_smtp_for_log(account, db),
        get_default_smtp_config(db),
    ])
    if not smtp_cfgs:
        return {"success": False, "message": "未配置 SMTP 服务器"}

    # 解析已有的 LLM 分析结果
    analyses = []
    if log.llm_raw_response:
        try:
            parsed = json.loads(log.llm_raw_response)
            if isinstance(parsed, list):
                analyses = parsed
            elif isinstance(parsed, dict):
                analyses = [parsed]
        except Exception:
            pass

    # 获取附件路径
    atts = db.query(Attachment).filter_by(log_id=log.id).all()
    attachment_paths = []
    for att in atts:
        if att.file_path:
            try:
                fp = resolve_attachment_path(att.file_path)
            except ValueError:
                logger.warning(f"附件路径越界，跳过: {att.file_path}")
                continue
            if fp.exists():
                attachment_paths.append(str(fp))

    # 获取输出模式
    mode_cfg = db.query(DefaultConfig).filter_by(key="analysis_output_mode").first()
    analysis_output_mode = mode_cfg.value if mode_cfg and mode_cfg.value else "content"

    # 解析目标邮箱
    targets = [t.strip() for t in (log.target_email or "").split(",") if t.strip()]
    if not targets:
        return {"success": False, "message": "未配置转发目标邮箱"}

    # 逐个转发（按候选发件服务器 failover：收件邮箱 SMTP 不可用时回退默认 SMTP）
    all_success = True
    last_error = ""
    for target in targets:
        success = False
        error_detail = "无可用 SMTP 候选"
        for idx, smtp in enumerate(smtp_cfgs):
            success, error_detail = forward_email(
                smtp_host=smtp["host"],
                smtp_port=smtp["port"],
                smtp_username=smtp["username"],
                smtp_password_encrypted=smtp["password_encrypted"],
                from_email=smtp["username"],
                to_email=target,
                to_name="",
                original_subject=log.subject or "",
                original_body=log.body_text or log.body_preview or "",
                original_sender=log.sender or "",
                original_recipient=log.recipient or "",
                original_date=log.received_at,
                analyses_results=analyses,
                attachment_paths=attachment_paths,
                analysis_output_mode=analysis_output_mode,
            )
            if success:
                break
            if idx < len(smtp_cfgs) - 1:
                logger.warning(
                    f"发件服务器 {smtp['host']}:{smtp['port']} 不可用，"
                    f"自动回退下一个 SMTP → {target} | 原因: {error_detail}"
                )
        if not success:
            all_success = False
            hosts = " / ".join(f"{c['host']}:{c['port']}" for c in smtp_cfgs)
            last_error = f"SMTP: {hosts} → {target} | 原因: {error_detail}"
            logger.error(f"重新转发失败: {last_error}")

    # 更新状态
    log.error_message = last_error if not all_success else None
    log.status = "forwarded" if all_success else "failed"
    db_retry_commit(db)

    return {
        "success": all_success,
        "message": "重新转发成功" if all_success else f"重新转发失败: {last_error}",
    }


@router.post("/delete-selected")
async def delete_selected_logs(
    request: Request,
    db: Session = Depends(get_db),
    log_ids: str = Form(""),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """批量删除选中的处理记录及其附件"""
    check_csrf(request, form_csrf)
    import shutil
    import time
    from sqlalchemy.exc import OperationalError

    if not log_ids.strip():
        return {"success": False, "message": "未选择任何记录"}

    try:
        ids = [int(x.strip()) for x in log_ids.split(",") if x.strip()]
    except ValueError:
        return {"success": False, "message": "无效的记录ID"}

    if not ids:
        return {"success": False, "message": "未选择任何记录"}

    # 先查询要删除的附件文件路径
    attachments = db.query(Attachment).filter(Attachment.log_id.in_(ids)).all()
    file_paths = []
    for att in attachments:
        if not att.file_path:
            continue
        try:
            file_paths.append(resolve_attachment_path(att.file_path))
        except ValueError:
            logger.warning(f"附件路径越界，跳过删除: {att.file_path}")

    # 数据库删除 — 带重试
    max_retries = 4
    for attempt in range(max_retries):
        try:
            # ⚠️ 删除顺序：必须先清掉引用 email_logs 的子表记录。
            # 连接启用了 PRAGMA foreign_keys=ON，残留引用会直接报
            # FOREIGN KEY constraint failed 导致整个删除失败。
            db.query(ForwardRecord).filter(ForwardRecord.log_id.in_(ids)).delete(synchronize_session=False)
            db.query(Attachment).filter(Attachment.log_id.in_(ids)).delete(synchronize_session=False)
            db.query(EmailLog).filter(EmailLog.id.in_(ids)).delete(synchronize_session=False)
            db.commit()
            break
        except OperationalError as e:
            db.rollback()
            if "database is locked" in str(e) and attempt < max_retries - 1:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise

    # 删除附件文件
    deleted_files = 0
    for fp in file_paths:
        try:
            if fp.exists():
                fp.unlink()
                deleted_files += 1
        except Exception:
            pass

    return {
        "success": True,
        "message": f"已删除 {len(ids)} 条记录（{deleted_files} 个附件文件）",
        "count": len(ids),
    }


@router.post("/clear")
async def clear_logs(request: Request, form_csrf: str = Form("", alias="_csrf_token"), db: Session = Depends(get_db)):
    """清除所有处理记录和附件（含重试，防并发写锁）"""
    check_csrf(request, form_csrf)
    import shutil
    import time
    from sqlalchemy.exc import OperationalError

    count = db.query(EmailLog).count()
    if count == 0:
        return {"success": True, "message": "没有记录需要清除", "count": 0}

    # 数据库删除 — 带重试，防止与调度器写锁冲突
    max_retries = 4
    for attempt in range(max_retries):
        try:
            # 子表先删（外键约束见 delete_selected_logs 中的说明）
            db.query(ForwardRecord).delete()
            db.query(Attachment).delete()
            db.query(EmailLog).delete()
            db.commit()
            break
        except OperationalError as e:
            db.rollback()
            if "database is locked" in str(e) and attempt < max_retries - 1:
                wait = 0.5 * (2 ** attempt)  # 0.5s, 1s, 2s, 4s
                time.sleep(wait)
                continue
            raise

    # DB 提交成功后再删除附件文件（防止回滚后文件丢失）
    att_dir = ATTACHMENTS_DIR
    if att_dir.exists():
        shutil.rmtree(att_dir)
        att_dir.mkdir(parents=True, exist_ok=True)

    return {"success": True, "message": f"已清除 {count} 条记录", "count": count}


# CSV 公式注入防护：以 = + - @ 开头的单元格在 Excel 中会被当作公式执行，
# 对来自外部邮件/LLM 的可控字段统一加 ' 前缀转义。
_DANGEROUS_PREFIXES = ("=", "+", "-", "@")


def _safe_cell(value: str) -> str:
    # 去除前导空白后仍以危险字符开头（如 " =cmd"）也可能被 Excel 当公式执行
    if value.lstrip().startswith(_DANGEROUS_PREFIXES):
        return "'" + value
    return value


@router.get("/export-csv")
async def export_csv(
    db: Session = Depends(get_db),
    date_from: str = Query("", description="开始日期 YYYY-MM-DD"),
    date_to: str = Query("", description="结束日期 YYYY-MM-DD"),
):
    """导出处理记录为 CSV 文件"""
    query = db.query(EmailLog).order_by(EmailLog.created_at.desc())

    if date_from:
        try:
            dt_from = datetime.strptime(date_from, "%Y-%m-%d")
            query = query.filter(EmailLog.created_at >= dt_from)
        except ValueError:
            pass

    if date_to:
        try:
            dt_to = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            query = query.filter(EmailLog.created_at <= dt_to)
        except ValueError:
            pass

    logs = query.all()

    # 预加载附件信息
    log_ids = [log.id for log in logs]
    attachment_map = {}
    if log_ids:
        atts = db.query(Attachment).filter(Attachment.log_id.in_(log_ids)).all()
        for att in atts:
            attachment_map.setdefault(att.log_id, []).append(att)

    output = io.StringIO()
    output.write('\ufeff')  # UTF-8 BOM for Excel compatibility
    writer = csv.writer(output)

    writer.writerow([
        "发件人", "发件人地址", "收件时间", "主题", "分类", "状态", "转发邮箱",
        "案件摘要", "AI解读", "涉及方", "错误信息", "邮件正文", "附件"
    ])

    for log in logs:
        atts = attachment_map.get(log.id, [])
        attachment_names = "; ".join(att.filename for att in atts) if atts else ""

        # 状态中文映射
        status_map = {
            "pending": "待处理", "analyzed": "已分析",
            "forwarded": "已转发", "failed": "失败", "skipped": "已跳过"
        }

        # 提取发件人名称和邮箱
        sender_raw = log.sender or ""
        sender_name = ""
        sender_email = sender_raw
        import re
        email_match = re.search(r'<([^>]+)>', sender_raw)
        if email_match:
            sender_email = email_match.group(1).strip()
            sender_name = sender_raw[:email_match.start()].strip().strip('"').strip("'").strip()
        else:
            sender_email = sender_raw.strip()
            sender_name = ""

        writer.writerow([
            _safe_cell(sender_name or sender_raw),
            _safe_cell(sender_email),
            log.received_at.strftime("%Y-%m-%d %H:%M:%S") if log.received_at else "",
            _safe_cell(log.subject or ""),
            _safe_cell(log.doc_type or ""),
            _safe_cell(status_map.get(log.status, log.status or "")),
            _safe_cell(log.target_email or ""),
            _safe_cell(log.case_summary or ""),
            _safe_cell(log.ai_interpretation or ""),
            _safe_cell(log.involved_parties or ""),
            _safe_cell(log.error_message or ""),
            _safe_cell(log.body_text or log.body_preview or ""),
            _safe_cell(attachment_names),
        ])

    output.seek(0)
    filename = f"legal-mail-logs-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/export-attachments")
async def export_attachments(
    db: Session = Depends(get_db),
    date_from: str = Query("", description="开始日期 YYYY-MM-DD"),
    date_to: str = Query("", description="结束日期 YYYY-MM-DD"),
):
    """按时间段导出所有附件为压缩包"""
    query = db.query(Attachment)

    if date_from or date_to:
        # 需要关联 EmailLog 来按日期筛选
        query = query.join(EmailLog).order_by(EmailLog.created_at.desc())

    if date_from:
        try:
            dt_from = datetime.strptime(date_from, "%Y-%m-%d")
            query = query.filter(EmailLog.created_at >= dt_from)
        except ValueError:
            pass

    if date_to:
        try:
            dt_to = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            query = query.filter(EmailLog.created_at <= dt_to)
        except ValueError:
            pass

    attachments = query.all()

    if not attachments:
        # 返回空 ZIP 提示
        return StreamingResponse(
            iter([b"PK\x05\x06\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"]),
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=no-attachments.zip"},
        )

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for att in attachments:
            if not att.file_path:
                continue

            # file_path 存储的是相对或绝对路径，resolve_attachment_path 统一处理
            try:
                full_path = resolve_attachment_path(att.file_path)
            except ValueError:
                logger.warning(f"附件路径越界，跳过导出: {att.file_path}")
                continue
            if not full_path.exists():
                continue

            # ZIP 内的路径使用 attachments/YYYY-MM-DD/{log_id}/filename 结构
            zip_path = str(att.file_path)
            zf.write(full_path, zip_path)

    zip_buffer.seek(0)
    filename = f"legal-mail-attachments-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"

    return StreamingResponse(
        iter([zip_buffer.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
