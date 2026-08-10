"""
仪表盘路由
"""
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db, db_retry_commit
from app.models import EmailAccount, EmailLog, DefaultConfig
from app.csrf import check_csrf
router = APIRouter(prefix="/dashboard", tags=["仪表盘"])


def _get_default_interval(db: Session) -> int:
    cfg = db.query(DefaultConfig).filter_by(key="default_check_interval").first()
    return int(cfg.value) if cfg else 30


@router.get("")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    """仪表盘首页"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_count = db.query(func.count(EmailLog.id)).filter(EmailLog.created_at >= today).scalar()
    forwarded_count = db.query(func.count(EmailLog.id)).filter(
        EmailLog.created_at >= today, EmailLog.status == "forwarded"
    ).scalar()
    failed_count = db.query(func.count(EmailLog.id)).filter(
        EmailLog.created_at >= today, EmailLog.status == "failed"
    ).scalar()
    skipped_count = db.query(func.count(EmailLog.id)).filter(
        EmailLog.created_at >= today, EmailLog.status == "skipped"
    ).scalar()

    accounts = db.query(EmailAccount).all()
    # 激活由「模型角色分配」决定：显示文书解读模型（第二阶段）
    from app.services.scheduler import _get_role_llm_cfg
    active_llm = _get_role_llm_cfg(db, "analyzer")
    recent_logs = db.query(EmailLog).order_by(EmailLog.created_at.desc()).limit(10).all()

    from app.services.scheduler import scheduler
    jobs = scheduler.get_jobs() if scheduler.running else []
    default_interval = _get_default_interval(db)

    return request.app.state.templates.TemplateResponse(request, "dashboard.html", {
        "request": request,
        "active_page": "dashboard",
        "today_count": today_count,
        "forwarded_count": forwarded_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "accounts": accounts,
        "active_llm": active_llm,
        "recent_logs": recent_logs,
        "scheduler_running": scheduler.running,
        "job_count": len(jobs),
        "default_interval": default_interval,
        "urgency_map": {"high": "\U0001f534", "medium": "\U0001f7e1", "low": "\U0001f7e2"},
    })


@router.post("/trigger-check")
async def trigger_manual_check(request: Request, form_csrf: str = Form("", alias="_csrf_token"), db: Session = Depends(get_db)):
    """手动触发收取分发（后台执行，立即返回）"""
    check_csrf(request, form_csrf)
    
    accounts = db.query(EmailAccount).filter_by(enabled=True).all()
    if not accounts:
        return {"success": False, "message": "没有启用的邮箱账户"}

    # 后台线程执行
    import threading
    import logging
    logger = logging.getLogger(__name__)
    from app.services.scheduler import check_account

    def run_checks():
        for acc in accounts:
            try:
                check_account(acc.id)
            except Exception as e:
                logger.error(f"手动触发检查账户 #{acc.id} 失败: {e}", exc_info=True)

    thread = threading.Thread(target=run_checks, daemon=True)
    thread.start()

    return {
        "success": True,
        "message": f"后台处理中... 共 {len(accounts)} 个账户，完成后刷新页面查看结果",
    }


@router.post("/settings")
async def save_settings(
    request: Request,
    db: Session = Depends(get_db),
    default_interval: int = Form(30),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """保存全局设置"""
    check_csrf(request, form_csrf)
    # 更新默认检查间隔
    cfg = db.query(DefaultConfig).filter_by(key="default_check_interval").first()
    if cfg:
        cfg.value = str(default_interval)
    else:
        db.add(DefaultConfig(key="default_check_interval", value=str(default_interval)))
    db_retry_commit(db)

    # ⚠️ 不再覆盖已有账户的独立检查间隔
    # 系统默认间隔仅影响新建账户的初始值

    return {"success": True, "message": f"已更新，默认间隔 {default_interval} 分钟"}


@router.get("/progress")
async def get_progress(request: Request):
    """获取当前执行进度"""
    from app.services.scheduler import get_progress as scheduler_progress
    return scheduler_progress()
