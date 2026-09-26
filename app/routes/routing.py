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


def _find_duplicate_rule(db: Session, rule_type: str, doc_type: str,
                         account_ids: str, target_email: str) -> bool:
    """检查是否已存在相同的 (rule_type, doc_type, account_ids, target_email) 规则"""
    from app.models import RoutingRule
    existing = db.query(RoutingRule).filter_by(
        rule_type=rule_type,
        doc_type=doc_type,
        account_ids=account_ids,
        target_email=target_email.strip(),
    ).first()
    return existing is not None


def _parse_doc_types(raw_list) -> str:
    """把表单多选的文书类型整理成逗号分隔串（去空、去重、保持提交顺序）"""
    seen = []
    for item in raw_list:
        name = (item or "").strip()
        if name and name not in seen:
            seen.append(name)
    return ",".join(seen)


def _normalize_rule_form(rule_type: str, doc_type_list) -> tuple[str, str, str | None]:
    """校验并规范化「匹配方式」相关表单字段。

    一条规则只能使用一种匹配方式：按邮箱时强制清空 doc_type，按类型时必须至少选一个类型。
    返回 (rule_type, doc_type, error_message)；error_message 非空表示校验失败。
    """
    rule_type = (rule_type or "account").strip()
    if rule_type not in ("account", "doc_type"):
        rule_type = "account"
    if rule_type == "account":
        return rule_type, "", None
    doc_type_str = _parse_doc_types(doc_type_list)
    if not doc_type_str:
        return rule_type, "", "按文书类型转发时，请至少选择一个文书类型"
    return rule_type, doc_type_str, None


@router.get("")
async def routing_page(request: Request, db: Session = Depends(get_db)):
    from app.services.llm_analyzer import _get_doc_types

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
        # 按类型规则的匹配内容（供列表展示）
        rule._rule_type = rule.rule_type or "account"
        rule._doc_type_names = [t for t in (rule.doc_type or "").split(",") if t.strip()]

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
        "doc_types": _get_doc_types(),
        "default_smtp": default_smtp,
        "scheduler_running": scheduler.running,
    })


@router.post("/add")
async def add_rule(
    request: Request,
    db: Session = Depends(get_db),
    target_email: str = Form(...),
    rule_type: str = Form("account"),
    enabled: str = Form("false"),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    check_csrf(request, form_csrf)
    form_data = await request.form()
    account_ids_list = form_data.getlist("account_ids")
    account_ids_str = ",".join(aid.strip() for aid in account_ids_list if aid.strip().isdigit())
    _enabled = enabled.lower() in ("true", "on", "1")

    # 匹配方式校验（一条规则只能用一种方式）
    _rule_type, _doc_type, err = _normalize_rule_form(rule_type, form_data.getlist("doc_type"))
    if err:
        flash(request, err, "warning")
        return RedirectResponse(url="/routing", status_code=303)

    # 重复检测
    existing = _find_duplicate_rule(db, _rule_type, _doc_type, account_ids_str, target_email.strip())
    if existing:
        flash(request, f"已存在相同规则（{target_email}），请勿重复添加", "warning")
        return RedirectResponse(url="/routing", status_code=303)

    rule = RoutingRule(
        rule_type=_rule_type,
        account_ids=account_ids_str,
        doc_type=_doc_type,
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
    rule_type: str = Form("account"),
    enabled: str = Form("false"),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    check_csrf(request, form_csrf)
    form_data = await request.form()
    account_ids_list = form_data.getlist("account_ids")
    account_ids_str = ",".join(aid.strip() for aid in account_ids_list if aid.strip().isdigit())
    _enabled = enabled.lower() in ("true", "on", "1")

    # 匹配方式校验（一条规则只能用一种方式）
    _rule_type, _doc_type, err = _normalize_rule_form(rule_type, form_data.getlist("doc_type"))
    if err:
        flash(request, err, "warning")
        return RedirectResponse(url="/routing", status_code=303)

    rule = db.query(RoutingRule).filter_by(id=rule_id).first()
    if not rule:
        return RedirectResponse(url="/routing", status_code=303)

    # 重复检测（排除自身）
    if (rule.rule_type != _rule_type or rule.doc_type != _doc_type
            or rule.target_email.strip() != target_email.strip()
            or rule.account_ids != account_ids_str):
        existing = _find_duplicate_rule(db, _rule_type, _doc_type, account_ids_str, target_email.strip())
        if existing and existing.id != rule.id:
            flash(request, f"已存在相同规则（{target_email}），请勿重复添加", "warning")
            return RedirectResponse(url="/routing", status_code=303)
    rule.rule_type = _rule_type
    rule.account_ids = account_ids_str
    rule.doc_type = _doc_type
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
