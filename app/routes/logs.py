"""
处理日志路由
"""
import csv
import io
import zipfile
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Query, Form
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models import EmailLog, Attachment
from app.scheduler import scheduler
from app.config import ATTACHMENTS_DIR, resolve_attachment_path
from app.csrf import check_csrf
from urllib.parse import quote
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

    return request.app.state.templates.TemplateResponse(request, "log_detail.html", {
        "request": request,
        "active_page": "logs",
        "log": log,
        "llm_result": llm_result,
        "attachments": attachments,
        "group_count": group_count,
        "scheduler_running": scheduler.running,
        "urgency_map": {"high": "🔴", "medium": "🟡", "low": "🟢"},
    })


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
    file_paths = [resolve_attachment_path(att.file_path) for att in attachments if att.file_path]

    # 数据库删除 — 带重试
    max_retries = 4
    for attempt in range(max_retries):
        try:
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
        "发件人", "收件时间", "主题", "分类", "状态", "转发邮箱",
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

        writer.writerow([
            log.sender or "",
            log.received_at.strftime("%Y-%m-%d %H:%M:%S") if log.received_at else "",
            log.subject or "",
            log.doc_type or "",
            status_map.get(log.status, log.status or ""),
            log.target_email or "",
            log.case_summary or "",
            log.ai_interpretation or "",
            log.involved_parties or "",
            log.error_message or "",
            log.body_text or log.body_preview or "",
            attachment_names,
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
            full_path = resolve_attachment_path(att.file_path)
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
