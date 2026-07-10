"""
路由规则配置 — 每个规则：监控邮箱 → 转发目标
"""
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db, db_retry_commit
from app.models import RoutingRule, DefaultConfig, EmailAccount
from app.config import encrypt
from app.flash import flash
from app.csrf import check_csrf
from app.services.scheduler import scheduler
router = APIRouter(prefix="/routing", tags=["路由规则"])


def _find_duplicate_rule(db: Session, account_ids: str, target_email: str) -> bool:
    """检查是否已存在相同的 (account_ids, target_email) 规则"""
    from app.models import RoutingRule
    existing = db.query(RoutingRule).filter_by(
        account_ids=account_ids,
        target_email=target_email.strip(),
    ).first()
    return existing is not None


@router.get("")
async def routing_page(request: Request, db: Session = Depends(get_db)):
    rules = db.query(RoutingRule).order_by(RoutingRule.id.desc()).all()
    accounts = db.query(EmailAccount).order_by(EmailAccount.name).all()

    account_map = {acc.id: acc for acc in accounts}
    for rule in rules:
        if rule.account_ids and rule.account_ids.strip():
            ids = [int(x) for x in rule.account_ids.split(",") if x.strip().isdigit()]
            rule._account_names = [
                account_map[aid].name for aid in ids if aid in account_map
            ]
        else:
            rule._account_names = []

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
        "account_map": account_map,
        "default_smtp": default_smtp,
        "scheduler_running": scheduler.running,
    })


@router.post("/add")
async def add_rule(
    request: Request,
    db: Session = Depends(get_db),
    target_email: str = Form(...),
    enabled: str = Form("false"),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    check_csrf(request, form_csrf)
    form_data = await request.form()
    account_ids_list = form_data.getlist("account_ids")
    account_ids_str = ",".join(aid.strip() for aid in account_ids_list if aid.strip().isdigit())
    _enabled = enabled.lower() in ("true", "on", "1")

    # 重复检测
    existing = _find_duplicate_rule(db, account_ids_str, target_email.strip())
    if existing:
        flash(request, f"已存在相同规则（{target_email}），请勿重复添加", "warning")
        return RedirectResponse(url="/routing", status_code=303)

    rule = RoutingRule(
        account_ids=account_ids_str,
        target_email=target_email.strip(),
        enabled=_enabled,
    )
    db.add(rule)
    db_retry_commit(db)
    flash(request, f"转发规则已添加 → {target_email}", "success")
    return RedirectResponse(url="/routing", status_code=303)


@router.post("/edit/{rule_id}")
async def edit_rule(
    rule_id: int,
    request: Request,
    db: Session = Depends(get_db),
    target_email: str = Form(...),
    enabled: str = Form("false"),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    check_csrf(request, form_csrf)
    form_data = await request.form()
    account_ids_list = form_data.getlist("account_ids")
    account_ids_str = ",".join(aid.strip() for aid in account_ids_list if aid.strip().isdigit())
    _enabled = enabled.lower() in ("true", "on", "1")

    rule = db.query(RoutingRule).filter_by(id=rule_id).first()
    if not rule:
        return RedirectResponse(url="/routing", status_code=303)

    # 重复检测（排除自身）
    if rule.target_email.strip() != target_email.strip() or rule.account_ids != account_ids_str:
        existing = _find_duplicate_rule(db, account_ids_str, target_email.strip())
        if existing:
            flash(request, f"已存在相同规则（{target_email}），请勿重复添加", "warning")
            return RedirectResponse(url="/routing", status_code=303)
    rule.account_ids = account_ids_str
    rule.target_email = target_email.strip()
    rule.enabled = _enabled
    db_retry_commit(db)
    flash(request, f"转发规则已更新 → {target_email}", "success")
    return RedirectResponse(url="/routing", status_code=303)


@router.post("/delete/{rule_id}")
async def delete_rule(
    rule_id: int,
    request: Request,
    db: Session = Depends(get_db),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    check_csrf(request, form_csrf)
    rule = db.query(RoutingRule).filter_by(id=rule_id).first()
    if rule:
        db.delete(rule)
        db_retry_commit(db)
        flash(request, "转发规则已删除", "success")
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
