"""
LLM API 配置路由
"""
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import LLMConfig
from app.config import encrypt, decrypt
from app.flash import flash
from app.csrf import check_csrf
from app.scheduler import scheduler
router = APIRouter(prefix="/llm-config", tags=["LLM配置"])


@router.get("")
async def llm_config_page(request: Request, db: Session = Depends(get_db)):
    configs = db.query(LLMConfig).order_by(LLMConfig.created_at.desc()).all()
    return request.app.state.templates.TemplateResponse(request, "llm_config.html", {
        "request": request,
        "active_page": "llm_config",
        "configs": configs,
        "scheduler_running": scheduler.running,
    })


@router.post("/add")
async def add_llm_config(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form("默认配置"),
    api_url: str = Form(...),
    api_key: str = Form(...),
    model_name: str = Form(...),
    analysis_prompt: str = Form(""),
    max_tokens: int = Form(2000),
    temperature: float = Form(0.3),
    is_active: str = Form("false"),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    check_csrf(request, form_csrf)
    _is_active = is_active.lower() in ("true", "on", "1")
    # 如果设为激活，先取消其他配置的激活状态
    if _is_active:
        db.query(LLMConfig).filter_by(is_active=True).update({"is_active": False})

    config = LLMConfig(
        name=name,
        api_url=api_url,
        api_key_encrypted=encrypt(api_key),
        model_name=model_name,
        analysis_prompt=analysis_prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        is_active=_is_active,
    )
    db.add(config)
    db.commit()
    flash(request, f"LLM 配置「{name}」已添加", "success")
    return RedirectResponse(url="/llm-config", status_code=303)


@router.post("/edit/{config_id}")
async def edit_llm_config(
    config_id: int,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form("默认配置"),
    api_url: str = Form(...),
    api_key: str = Form(""),
    model_name: str = Form(...),
    analysis_prompt: str = Form(""),
    max_tokens: int = Form(2000),
    temperature: float = Form(0.3),
    is_active: str = Form("false"),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    check_csrf(request, form_csrf)
    _is_active = is_active.lower() in ("true", "on", "1")
    config = db.query(LLMConfig).filter_by(id=config_id).first()
    if not config:
        return RedirectResponse(url="/llm-config", status_code=303)

    if _is_active:
        db.query(LLMConfig).filter_by(is_active=True).update({"is_active": False})

    config.name = name
    config.api_url = api_url
    if api_key.strip():
        config.api_key_encrypted = encrypt(api_key)
    config.model_name = model_name
    config.analysis_prompt = analysis_prompt
    config.max_tokens = max_tokens
    config.temperature = temperature
    config.is_active = _is_active
    db.commit()
    flash(request, f"LLM 配置「{config.name}」已更新", "success")
    return RedirectResponse(url="/llm-config", status_code=303)


@router.post("/delete/{config_id}")
async def delete_llm_config(
    config_id: int,
    request: Request,
    db: Session = Depends(get_db),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    check_csrf(request, form_csrf)
    config = db.query(LLMConfig).filter_by(id=config_id).first()
    if config:
        db.delete(config)
        db.commit()
        flash(request, f"LLM 配置「{config.name}」已删除", "success")
    return RedirectResponse(url="/llm-config", status_code=303)


@router.post("/activate/{config_id}")
async def activate_llm_config(
    config_id: int,
    request: Request,
    db: Session = Depends(get_db),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    check_csrf(request, form_csrf)
    db.query(LLMConfig).filter_by(is_active=True).update({"is_active": False})
    config = db.query(LLMConfig).filter_by(id=config_id).first()
    if config:
        config.is_active = True
        db.commit()
        flash(request, f"已激活 LLM 配置「{config.name}」", "success")
    return RedirectResponse(url="/llm-config", status_code=303)


@router.post("/detect-model/{config_id}")
async def detect_model(request: Request, config_id: int, form_csrf: str = Form("", alias="_csrf_token"), db: Session = Depends(get_db)):
    """检测模型类型并更新配置"""
    check_csrf(request, form_csrf)
    from app.ocr import detect_model_type
    config = db.query(LLMConfig).filter_by(id=config_id).first()
    if not config:
        return {"success": False, "message": "配置不存在"}

    try:
        api_key = decrypt(config.api_key_encrypted)
        model_type = await detect_model_type(config.api_url, api_key, config.model_name)
        config.model_type = model_type
        db.commit()
        labels = {"multimodal": "多模态(支持图片)", "text": "纯文本", "unknown": "未知"}
        return {"success": True, "model_type": model_type, "label": labels.get(model_type, model_type)}
    except Exception as e:
        return {"success": False, "message": f"检测失败: {str(e)[:200]}"}


@router.post("/test/{config_id}")
async def test_llm(request: Request, config_id: int, form_csrf: str = Form("", alias="_csrf_token"), db: Session = Depends(get_db)):
    """测试 LLM 连接"""
    check_csrf(request, form_csrf)
    from app.llm_analyzer import analyze_email
    config = db.query(LLMConfig).filter_by(id=config_id).first()
    if not config:
        return {"success": False, "message": "配置不存在"}

    try:
        result = await analyze_email(
            api_url=config.api_url,
            api_key_encrypted=config.api_key_encrypted,
            model_name=config.model_name,
            subject="测试邮件",
            sender="test@example.com",
            body="这是一封测试邮件，用于验证LLM API连接是否正常。",
            custom_prompt=config.analysis_prompt,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
        )
        return {"success": True, "message": f"测试成功！\n返回：{result['case_summary']}"}
    except Exception as e:
        return {"success": False, "message": f"测试失败：{str(e)}"}


@router.get("/default-prompt")
async def get_default_prompt():
    """返回当前系统默认提示词模板（用于前端“恢复默认模板”按钮）"""
    from app.llm_analyzer import _get_default_prompt
    prompt = _get_default_prompt()
    return {"prompt": prompt}
