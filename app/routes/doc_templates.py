"""
文书模板管理 — CRUD 路由（支持按邮箱账户区分）
"""
import logging
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import DocTemplate, EmailAccount
from app.flash import flash
from app.csrf import check_csrf
from app.scheduler import scheduler

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/doc-templates", tags=["文书模板"])


@router.get("")
async def templates_page(request: Request, db: Session = Depends(get_db)):
    """文书模板管理页面"""
    templates = db.query(DocTemplate).order_by(
        DocTemplate.account_id.is_(None),  # 全局排前面
        DocTemplate.doc_type,
        DocTemplate.name,
    ).all()
    accounts = db.query(EmailAccount).order_by(EmailAccount.name).all()
    doc_types = sorted(set(t[0] for t in db.query(DocTemplate.doc_type).distinct().all()))
    return request.app.state.templates.TemplateResponse(request, "doc_templates.html", {
        "request": request,
        "active_page": "doc_templates",
        "templates": templates,
        "accounts": accounts,
        "doc_types": doc_types,
        "scheduler_running": scheduler.running,
    })


@router.post("/add")
async def add_template(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(""),
    doc_type: str = Form(""),
    content: str = Form(""),
    description: str = Form(""),
    account_id: str = Form(""),
    is_default: str = Form("false"),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """添加文书模板"""
    check_csrf(request, form_csrf)
    if not name.strip() or not doc_type.strip() or not content.strip():
        flash(request, "模板名称、文书类型和模板内容不能为空", "error")
        return RedirectResponse(url="/doc-templates", status_code=303)

    _account_id = int(account_id) if account_id.strip() else None
    _doc_type = doc_type.strip()

    # 检查同账户同类型是否已存在模板
    existing = db.query(DocTemplate).filter_by(account_id=_account_id, doc_type=_doc_type).first()
    if existing:
        label = f"账户「{existing.account.name}」" if _account_id else "全局"
        flash(request, f"{label}下「{_doc_type}」类型的模板已存在（{existing.name}），请先删除再添加", "error")
        return RedirectResponse(url="/doc-templates", status_code=303)

    _is_default = is_default.lower() in ("true", "on", "1")
    if _is_default:
        # 取消同账户同类型的其他默认模板
        db.query(DocTemplate).filter_by(
            account_id=_account_id, doc_type=_doc_type, is_default=True
        ).update({"is_default": False})

    db.add(DocTemplate(
        name=name.strip(),
        doc_type=_doc_type,
        content=content.strip(),
        description=description.strip(),
        account_id=_account_id,
        is_default=_is_default,
    ))
    db.commit()
    flash(request, f"模板「{name}」已添加", "success")
    return RedirectResponse(url="/doc-templates", status_code=303)


@router.post("/edit/{template_id}")
async def edit_template(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(""),
    doc_type: str = Form(""),
    content: str = Form(""),
    description: str = Form(""),
    account_id: str = Form(""),
    is_default: str = Form("false"),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """编辑文书模板"""
    check_csrf(request, form_csrf)
    tmpl = db.query(DocTemplate).filter_by(id=template_id).first()
    if not tmpl:
        flash(request, "模板不存在", "error")
        return RedirectResponse(url="/doc-templates", status_code=303)

    _account_id = int(account_id) if account_id.strip() else None
    _doc_type = doc_type.strip() or tmpl.doc_type

    # 检查是否与同账户同类型的其他模板冲突
    if _doc_type != tmpl.doc_type or _account_id != tmpl.account_id:
        conflict = db.query(DocTemplate).filter(
            DocTemplate.id != template_id,
            DocTemplate.account_id == _account_id,
            DocTemplate.doc_type == _doc_type,
        ).first()
        if conflict:
            label = f"账户「{conflict.account.name}」" if _account_id else "全局"
            flash(request, f"{label}下「{_doc_type}」类型的模板已存在（{conflict.name}），修改失败", "error")
            return RedirectResponse(url="/doc-templates", status_code=303)

    _is_default = is_default.lower() in ("true", "on", "1")
    if _is_default and not tmpl.is_default:
        db.query(DocTemplate).filter_by(
            account_id=_account_id, doc_type=_doc_type, is_default=True,
        ).update({"is_default": False})

    tmpl.name = name.strip() or tmpl.name
    tmpl.doc_type = _doc_type
    tmpl.content = content.strip() or tmpl.content
    tmpl.description = description.strip()
    tmpl.account_id = _account_id
    tmpl.is_default = _is_default
    db.commit()
    flash(request, f"模板「{tmpl.name}」已更新", "success")
    return RedirectResponse(url="/doc-templates", status_code=303)


@router.post("/delete/{template_id}")
async def delete_template(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """删除文书模板"""
    check_csrf(request, form_csrf)
    tmpl = db.query(DocTemplate).filter_by(id=template_id).first()
    if tmpl:
        db.delete(tmpl)
        db.commit()
        flash(request, f"模板「{tmpl.name}」已删除", "success")
    return RedirectResponse(url="/doc-templates", status_code=303)


@router.post("/set-default/{template_id}")
async def set_default_template(
    template_id: int,
    request: Request,
    db: Session = Depends(get_db),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """设为该文书类型的默认模板"""
    check_csrf(request, form_csrf)
    tmpl = db.query(DocTemplate).filter_by(id=template_id).first()
    if not tmpl:
        return JSONResponse({"success": False, "message": "模板不存在"}, status_code=404)

    db.query(DocTemplate).filter_by(
        account_id=tmpl.account_id, doc_type=tmpl.doc_type, is_default=True,
    ).update({"is_default": False})
    tmpl.is_default = True
    db.commit()
    return {"success": True, "message": f"「{tmpl.name}」已设为默认模板"}
