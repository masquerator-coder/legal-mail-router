"""
路由规则配置
"""
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db, db_retry_commit
from app.models import RoutingRule, DefaultConfig, EmailAccount
from app.config import encrypt
from app.flash import flash
from app.csrf import check_csrf
from app.scheduler import scheduler
router = APIRouter(prefix="/routing", tags=["路由规则"])


@router.get("")
async def routing_page(request: Request, db: Session = Depends(get_db)):
    rules = db.query(RoutingRule).order_by(RoutingRule.priority.desc()).all()
    accounts = db.query(EmailAccount).order_by(EmailAccount.name).all()

    # 默认SMTP配置
    default_smtp = {}
    for key in ["default_smtp_host", "default_smtp_port", "default_smtp_username",
                 "default_forward_email"]:
        cfg = db.query(DefaultConfig).filter_by(key=key).first()
        if cfg:
            default_smtp[key.replace("default_", "")] = cfg.value

    return request.app.state.templates.TemplateResponse(request, "routing.html", {
        "request": request,
        "active_page": "routing",
        "rules": rules,
        "accounts": accounts,
        "default_smtp": default_smtp,
        "scheduler_running": scheduler.running,
    })


@router.post("/add")
async def add_rule(
    request: Request,
    db: Session = Depends(get_db),
    doc_type: str = Form(...),
    keywords: str = Form(""),
    target_email: str = Form(...),
    target_name: str = Form(""),
    account_id: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    priority: int = Form(0),
    enabled: str = Form("false"),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    check_csrf(request, form_csrf)
    _enabled = enabled.lower() in ("true", "on", "1")
    rule = RoutingRule(
        doc_type=doc_type,
        keywords=keywords,
        target_email=target_email,
        target_name=target_name,
        account_id=int(account_id) if account_id.strip() else None,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_username=smtp_username,
        smtp_password_encrypted=encrypt(smtp_password) if smtp_password.strip() else "",
        priority=priority,
        enabled=_enabled,
    )
    db.add(rule)
    db_retry_commit(db)
    flash(request, f"路由规则「{doc_type}」已添加", "success")
    return RedirectResponse(url="/routing", status_code=303)


@router.post("/edit/{rule_id}")
async def edit_rule(
    rule_id: int,
    request: Request,
    db: Session = Depends(get_db),
    doc_type: str = Form(...),
    keywords: str = Form(""),
    target_email: str = Form(...),
    target_name: str = Form(""),
    account_id: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    priority: int = Form(0),
    enabled: str = Form("false"),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    check_csrf(request, form_csrf)
    _enabled = enabled.lower() in ("true", "on", "1")
    rule = db.query(RoutingRule).filter_by(id=rule_id).first()
    if not rule:
        return RedirectResponse(url="/routing", status_code=303)

    rule.doc_type = doc_type
    rule.keywords = keywords
    rule.target_email = target_email
    rule.target_name = target_name
    rule.account_id = int(account_id) if account_id.strip() else None
    rule.smtp_host = smtp_host
    rule.smtp_port = smtp_port
    rule.smtp_username = smtp_username
    if smtp_password.strip():
        rule.smtp_password_encrypted = encrypt(smtp_password)
    rule.priority = priority
    rule.enabled = _enabled
    db_retry_commit(db)
    flash(request, f"路由规则「{doc_type}」已更新", "success")
    return RedirectResponse(url="/routing", status_code=303)


@router.get("/delete/{rule_id}")
async def delete_rule(rule_id: int, request: Request, db: Session = Depends(get_db)):
    rule = db.query(RoutingRule).filter_by(id=rule_id).first()
    if rule:
        db.delete(rule)
        db_retry_commit(db)
        flash(request, f"路由规则「{rule.doc_type}」已删除", "success")
    return RedirectResponse(url="/routing", status_code=303)


@router.post("/default-smtp")
async def save_default_smtp(
    request: Request,
    db: Session = Depends(get_db),
    smtp_host: str = Form(""),
    smtp_port: str = Form("587"),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    forward_email: str = Form(""),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """保存默认 SMTP 配置"""
    check_csrf(request, form_csrf)
    configs = {
        "default_smtp_host": smtp_host,
        "default_smtp_port": smtp_port,
        "default_smtp_username": smtp_username,
        "default_forward_email": forward_email,
    }

    for key, value in configs.items():
        cfg = db.query(DefaultConfig).filter_by(key=key).first()
        if cfg:
            cfg.value = value
        else:
            db.add(DefaultConfig(key=key, value=value))

    # 密码只在输入时更新
    if smtp_password.strip():
        cfg = db.query(DefaultConfig).filter_by(key="default_smtp_password").first()
        encrypted = encrypt(smtp_password)
        if cfg:
            cfg.value = encrypted
        else:
            db.add(DefaultConfig(key="default_smtp_password", value=encrypted))

    db_retry_commit(db)
    flash(request, "默认 SMTP 配置已保存", "success")
    return RedirectResponse(url="/routing", status_code=303)
