"""OCR 配置路由"""
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from app.database import get_db, db_retry_commit
from app.models import OCRConfig
from app.config import encrypt, decrypt
from app.services.scheduler import scheduler
from app.flash import flash
from app.csrf import check_csrf
from app.services.ocr_test_data import PREBUILT_TEST_PDF

router = APIRouter(prefix="/ocr-config", tags=["OCR配置"])


@router.get("")
async def ocr_config_page(request: Request, db: Session = Depends(get_db)):
    """OCR 配置页面"""
    configs = db.query(OCRConfig).order_by(OCRConfig.created_at.desc()).all()
    return request.app.state.templates.TemplateResponse(request, "ocr_config.html", {
        "request": request,
        "active_page": "ocr_config",
        "configs": configs,
        "scheduler_running": scheduler.running,
    })


@router.post("/add")
async def add_ocr_config(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form("OCR配置"),
    provider_type: str = Form("paddleocr"),
    api_url: str = Form(...),
    api_key: str = Form(""),
    model_name: str = Form(""),
    pdf_capable: bool = Form(False),
    pdf_capable_manual: str = Form("auto"),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """添加 OCR 配置"""
    check_csrf(request, form_csrf)
    # 处理 pdf_capable: auto 模式不设值（留待测试确定）
    _pdf_capable = None
    if pdf_capable_manual == "yes":
        _pdf_capable = True
    elif pdf_capable_manual == "no":
        _pdf_capable = False
    # auto → None（未测）

    cfg = OCRConfig(
        name=name,
        provider_type=provider_type,
        api_url=api_url,
        api_key_encrypted=encrypt(api_key) if api_key else "",
        model_name=model_name,
        is_active=True,
        pdf_capable=_pdf_capable,
    )
    db.add(cfg)
    db_retry_commit(db)
    flash(request, f"OCR 配置「{name}」已添加", "success")
    return RedirectResponse(url="/ocr-config", status_code=303)


@router.post("/update/{cfg_id}")
async def update_ocr_config(
    cfg_id: int,
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form("OCR配置"),
    provider_type: str = Form("paddleocr"),
    api_url: str = Form(...),
    api_key: str = Form(""),
    model_name: str = Form(""),
    is_active: bool = Form(True),
    pdf_capable: bool = Form(False),
    pdf_capable_manual: str = Form("auto"),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """更新 OCR 配置"""
    check_csrf(request, form_csrf)
    cfg = db.query(OCRConfig).filter_by(id=cfg_id).first()
    if not cfg:
        flash(request, "配置不存在", "error")
        return RedirectResponse(url="/ocr-config", status_code=303)
    cfg.name = name
    cfg.provider_type = provider_type
    cfg.api_url = api_url
    if api_key:
        cfg.api_key_encrypted = encrypt(api_key)
    cfg.model_name = model_name
    cfg.is_active = is_active

    # 处理 pdf_capable_manual
    if pdf_capable_manual == "yes":
        cfg.pdf_capable = True
    elif pdf_capable_manual == "no":
        cfg.pdf_capable = False
    # auto: 保留已有值不动

    db_retry_commit(db)
    flash(request, f"OCR 配置「{cfg.name}」已更新", "success")
    return RedirectResponse(url="/ocr-config", status_code=303)


@router.post("/delete/{cfg_id}")
async def delete_ocr_config(
    cfg_id: int,
    request: Request,
    db: Session = Depends(get_db),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """删除 OCR 配置"""
    check_csrf(request, form_csrf)
    cfg = db.query(OCRConfig).filter_by(id=cfg_id).first()
    if not cfg:
        flash(request, "配置不存在", "error")
        return RedirectResponse(url="/ocr-config", status_code=303)
    db.delete(cfg)
    db_retry_commit(db)
    flash(request, f"OCR 配置「{cfg.name}」已删除", "success")
    return RedirectResponse(url="/ocr-config", status_code=303)


@router.post("/test/{cfg_id}")
async def test_ocr_config(
    cfg_id: int,
    request: Request,
    form_csrf: str = Form("", alias="_csrf_token"),
    db: Session = Depends(get_db),
):
    """两步测试：PNG 连通性 → PDF 能力"""
    check_csrf(request, form_csrf)
    cfg = db.query(OCRConfig).filter_by(id=cfg_id).first()
    if not cfg:
        return JSONResponse({"success": False, "message": "配置不存在"})

    results = {"connectivity_ok": False, "pdf_capable": None, "details": []}

    ocr_cfg = {
        "provider_type": cfg.provider_type,
        "api_url": cfg.api_url,
        "api_key": decrypt(cfg.api_key_encrypted) if cfg.api_key_encrypted else "",
        "model_name": cfg.model_name,
    }

    # ─── 第1步：测试 PNG 连通性 ───
    try:
        from app.services.ocr import ocr_image
        from app.services.ocr_test_data import PREBUILT_TEST_PNG
        await ocr_image(PREBUILT_TEST_PNG, ocr_cfg, "test.png")
        results["connectivity_ok"] = True
        results["details"].append("✅ PNG 连通性测试通过")
    except Exception as e:
        results["details"].append(f"❌ PNG 连通性测试失败: {str(e)[:200]}")
        # 连通性不通 → 不继续测 PDF，直接返回
        _save_test_results(db, cfg, connectivity_ok=False, pdf_capable=None)
        return JSONResponse({
            "success": False,
            "message": f"连接失败: {str(e)[:200]}",
            "details": results["details"],
            "connectivity_ok": False,
        })

    # ─── 第2步：测试 PDF 识别能力 ───
    # 对已知类型用静态判断（节省一次网络请求）
    if cfg.provider_type in ("paddleocr", "openai-vision"):
        results["pdf_capable"] = False
        results["details"].append(f"🔲 已知类型 {cfg.provider_type} 不支持 PDF 直读")
    elif cfg.provider_type == "mineru":
        results["pdf_capable"] = True
        results["details"].append("📄 MinerU 支持 PDF 直读")
    else:
        # custom / 未知类型 → 发预制测试 PDF 探测
        try:
            from app.services.ocr import ocr_pdf
            text = await ocr_pdf(PREBUILT_TEST_PDF, ocr_cfg, "test.pdf")
            if text and "OCR-PDF-TEST-2024" in text:
                results["pdf_capable"] = True
                results["details"].append("📄 探测成功：该服务支持 PDF 直读")
            else:
                results["pdf_capable"] = False
                results["details"].append("🔲 探测结果：该服务不支持 PDF 直读" +
                    (f"（返回: {text[:60]}）" if text else "（返回空）"))
        except Exception as e:
            # PDF 请求报错 → 视为不支持
            results["pdf_capable"] = False
            results["details"].append(f"🔲 PDF 请求失败，视为不支持 PDF 直读: {str(e)[:100]}")

    # 保存检测结果到数据库
    _save_test_results(db, cfg, connectivity_ok=results["connectivity_ok"],
                        pdf_capable=results["pdf_capable"])

    pdf_status = "支持 PDF 直读 📄" if results["pdf_capable"] else \
                 "仅支持图片 🔲" if results["pdf_capable"] is False else "未知 ❓"

    return JSONResponse({
        "success": True,
        "message": f"OCR 测试完成 ({cfg.provider_type}) — {pdf_status}",
        "details": results["details"],
        "connectivity_ok": results["connectivity_ok"],
        "pdf_capable": results["pdf_capable"],
    })


def _save_test_results(db, cfg, connectivity_ok: bool | None, pdf_capable: bool | None):
    """将测试结果写入数据库（事务独立的会话）"""
    # 直接使用路由传来的 db 会触发 autoflush，用单独会话避免干扰
    from app.database import SessionLocal
    s = SessionLocal()
    try:
        row = s.query(OCRConfig).filter_by(id=cfg.id).first()
        if row:
            row.connectivity_ok = connectivity_ok
            # 仅当 pdf_capable 值有变化时才覆盖（保留用户手工覆盖的可能）
            if pdf_capable is not None and row.pdf_capable is None:
                row.pdf_capable = pdf_capable
            s.commit()
    except Exception:
        s.rollback()
    finally:
        s.close()
