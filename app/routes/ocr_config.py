"""
OCR 配置路由
"""
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import OCRConfig
from app.config import encrypt, decrypt
from app.scheduler import scheduler
from app.flash import flash
from app.csrf import check_csrf
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
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """添加 OCR 配置"""
    check_csrf(request, form_csrf)
    cfg = OCRConfig(
        name=name,
        provider_type=provider_type,
        api_url=api_url,
        api_key_encrypted=encrypt(api_key) if api_key else "",
        model_name=model_name,
        is_active=True,
    )
    db.add(cfg)
    db.commit()
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
    db.commit()
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
    db.commit()
    flash(request, f"OCR 配置「{cfg.name}」已删除", "success")
    return RedirectResponse(url="/ocr-config", status_code=303)


@router.post("/test/{cfg_id}")
async def test_ocr_config(cfg_id: int, request: Request, form_csrf: str = Form("", alias="_csrf_token"), db: Session = Depends(get_db)):
    """测试 OCR 连接"""
    check_csrf(request, form_csrf)
    cfg = db.query(OCRConfig).filter_by(id=cfg_id).first()
    if not cfg:
        return {"success": False, "message": "配置不存在"}

    try:
        # 生成 200x50 白色 PNG 做测试（1x1 透明图会触发 PaddleOCR 500 错误）
        import struct
        import zlib
        def _make_test_png():
            w, h = 200, 50
            def _chunk(ctype, data):
                c = ctype + data
                return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
            header = b'\x89PNG\r\n\x1a\n'
            ihdr = _chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
            raw = b''
            for y in range(h):
                raw += b'\x00'  # filter none
                for x in range(w):
                    raw += b'\xff\xff\xff'  # white
            idat = _chunk(b'IDAT', zlib.compress(raw))
            iend = _chunk(b'IEND', b'')
            return header + ihdr + idat + iend
        tiny_png = _make_test_png()

        ocr_cfg = {
            "provider_type": cfg.provider_type,
            "api_url": cfg.api_url,
            "api_key": decrypt(cfg.api_key_encrypted) if cfg.api_key_encrypted else "",
            "model_name": cfg.model_name,
        }

        from app.ocr import ocr_image
        await ocr_image(tiny_png, ocr_cfg, "test.png")
        return {"success": True, "message": f"OCR 连接成功 ({cfg.provider_type})"}
    except Exception as e:
        return {"success": False, "message": f"连接失败: {str(e)[:200]}"}
