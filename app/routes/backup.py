"""
配置备份与恢复
"""
import json
import logging
from datetime import datetime
from fastapi import APIRouter, Request, Depends, Form, UploadFile, File
from fastapi.responses import StreamingResponse, RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db, db_retry_commit
from app.models import EmailAccount, LLMConfig, OCRConfig, RoutingRule, DefaultConfig
from app.flash import flash
from app.csrf import check_csrf
import io

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/backup", tags=["配置备份"])


@router.get("")
async def backup_page(request: Request):
    """备份页面 — 重定向到系统设置"""
    return RedirectResponse(url="/settings", status_code=302)


@router.get("/export")
async def export_backup(db: Session = Depends(get_db)):
    """导出所有配置为 JSON 文件（加密字段保持密文）"""
    data = {
        "version": 1,
        "exported_at": datetime.now().isoformat(),
        "system_name": "文书分发系统",
        "email_accounts": [],
        "llm_config": [],
        "ocr_config": [],
        "routing_rules": [],
        "system_settings": [],
    }

    # 邮箱账户
    for acc in db.query(EmailAccount).order_by(EmailAccount.id).all():
        data["email_accounts"].append({
            "name": acc.name,
            "imap_host": acc.imap_host,
            "imap_port": acc.imap_port,
            "use_ssl": acc.use_ssl,
            "provider_type": acc.provider_type,
            "username": acc.username,
            "password_encrypted": acc.password_encrypted,
            "check_interval": acc.check_interval,
            "filter_sender": acc.filter_sender,
            "download_attachments": acc.download_attachments,
            "enabled": acc.enabled,
        })

    # LLM 配置
    for cfg in db.query(LLMConfig).order_by(LLMConfig.id).all():
        data["llm_config"].append({
            "name": cfg.name,
            "api_url": cfg.api_url,
            "api_key_encrypted": cfg.api_key_encrypted,
            "model_name": cfg.model_name,
            "analysis_prompt": cfg.analysis_prompt,
            "max_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
            "is_active": cfg.is_active,
            "model_type": cfg.model_type,
        })

    # OCR 配置
    for cfg in db.query(OCRConfig).order_by(OCRConfig.id).all():
        data["ocr_config"].append({
            "name": cfg.name,
            "provider_type": cfg.provider_type,
            "api_url": cfg.api_url,
            "api_key_encrypted": cfg.api_key_encrypted,
            "model_name": cfg.model_name,
            "is_active": cfg.is_active,
        })

    # 路由规则
    for rule in db.query(RoutingRule).order_by(RoutingRule.id).all():
        # 查找账户名
        account_name = None
        if rule.account_id:
            acc = db.query(EmailAccount).filter_by(id=rule.account_id).first()
            if acc:
                account_name = acc.name
        data["routing_rules"].append({
            "doc_type": rule.doc_type,
            "keywords": rule.keywords,
            "target_email": rule.target_email,
            "target_name": rule.target_name,
            "account_name": account_name,
            "smtp_host": rule.smtp_host,
            "smtp_port": rule.smtp_port,
            "smtp_username": rule.smtp_username,
            "smtp_password_encrypted": rule.smtp_password_encrypted,
            "priority": rule.priority,
            "enabled": rule.enabled,
        })

    # 系统设置
    for cfg in db.query(DefaultConfig).order_by(DefaultConfig.key).all():
        data["system_settings"].append({
            "key": cfg.key,
            "value": cfg.value,
        })

    json_bytes = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    filename = f"legal-mail-router-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"

    return StreamingResponse(
        io.BytesIO(json_bytes),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import")
async def import_backup(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """从 JSON 文件恢复配置"""
    check_csrf(request, form_csrf)

    if not file.filename.endswith(".json"):
        flash(request, "请上传 .json 格式的备份文件", "error")
        return RedirectResponse(url="/backup", status_code=303)

    try:
        content = await file.read()
        data = json.loads(content.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        flash(request, f"备份文件格式错误: {e}", "error")
        return RedirectResponse(url="/backup", status_code=303)

    if "version" not in data:
        flash(request, "无效的备份文件：缺少 version 字段", "error")
        return RedirectResponse(url="/backup", status_code=303)

    stats = {"accounts": 0, "llm": 0, "ocr": 0, "rules": 0, "settings": 0}

    try:
        # 先建立账户名→ID 映射（用于关联路由规则）
        account_map = {}  # name → id

        # 导入邮箱账户 — 按 name 匹配，存在则更新，不存在则创建
        for item in data.get("email_accounts", []):
            name = item["name"]
            existing = db.query(EmailAccount).filter_by(name=name).first()
            if existing:
                existing.imap_host = item["imap_host"]
                existing.imap_port = item.get("imap_port", 993)
                existing.use_ssl = item.get("use_ssl", True)
                existing.provider_type = item.get("provider_type", "auto")
                existing.username = item["username"]
                existing.password_encrypted = item["password_encrypted"]
                existing.check_interval = item.get("check_interval", 30)
                existing.filter_sender = item.get("filter_sender", "")
                existing.download_attachments = item.get("download_attachments", True)
                existing.enabled = item.get("enabled", True)
                db.flush()
                account_map[name] = existing.id
                stats["accounts"] += 1
            else:
                acc = EmailAccount(
                    name=name,
                    imap_host=item["imap_host"],
                    imap_port=item.get("imap_port", 993),
                    use_ssl=item.get("use_ssl", True),
                    provider_type=item.get("provider_type", "auto"),
                    username=item["username"],
                    password_encrypted=item["password_encrypted"],
                    check_interval=item.get("check_interval", 30),
                    filter_sender=item.get("filter_sender", ""),
                    download_attachments=item.get("download_attachments", True),
                    enabled=item.get("enabled", True),
                )
                db.add(acc)
                db.flush()
                account_map[name] = acc.id
                stats["accounts"] += 1

        # 导入 LLM 配置 — 按 name 匹配，存在则更新，不存在则创建
        for item in data.get("llm_config", []):
            llm_name = item.get("name", "默认配置")
            existing = db.query(LLMConfig).filter_by(name=llm_name).first()
            if existing:
                existing.api_url = item["api_url"]
                existing.api_key_encrypted = item["api_key_encrypted"]
                existing.model_name = item["model_name"]
                existing.analysis_prompt = item.get("analysis_prompt", "")
                existing.max_tokens = item.get("max_tokens", 2000)
                existing.temperature = item.get("temperature", 0.3)
                existing.is_active = item.get("is_active", True)
                existing.model_type = item.get("model_type", "unknown")
            else:
                cfg = LLMConfig(
                    name=llm_name,
                    api_url=item["api_url"],
                    api_key_encrypted=item["api_key_encrypted"],
                    model_name=item["model_name"],
                    analysis_prompt=item.get("analysis_prompt", ""),
                    max_tokens=item.get("max_tokens", 2000),
                    temperature=item.get("temperature", 0.3),
                    is_active=item.get("is_active", True),
                    model_type=item.get("model_type", "unknown"),
                )
                db.add(cfg)
            stats["llm"] += 1

        # 导入 OCR 配置 — 按 name 匹配，存在则更新，不存在则创建
        for item in data.get("ocr_config", []):
            ocr_name = item.get("name", "OCR配置")
            existing = db.query(OCRConfig).filter_by(name=ocr_name).first()
            if existing:
                existing.provider_type = item.get("provider_type", "paddleocr")
                existing.api_url = item["api_url"]
                existing.api_key_encrypted = item.get("api_key_encrypted", "")
                existing.model_name = item.get("model_name", "")
                existing.is_active = item.get("is_active", True)
            else:
                cfg = OCRConfig(
                    name=ocr_name,
                    provider_type=item.get("provider_type", "paddleocr"),
                    api_url=item["api_url"],
                    api_key_encrypted=item.get("api_key_encrypted", ""),
                    model_name=item.get("model_name", ""),
                    is_active=item.get("is_active", True),
                )
                db.add(cfg)
            stats["ocr"] += 1

        # 导入路由规则 — 按 (account_id, doc_type, target_email) 三元组匹配，存在则更新，不存在则创建
        for item in data.get("routing_rules", []):
            account_id = None
            if item.get("account_name"):
                account_id = account_map.get(item["account_name"])
            doc_type = item["doc_type"]
            target_email = item["target_email"]
            # 匹配已有规则：同账户 + 同文书类型 + 同目标邮箱
            query = db.query(RoutingRule).filter_by(
                doc_type=doc_type, target_email=target_email
            )
            if account_id is not None:
                query = query.filter_by(account_id=account_id)
            else:
                query = query.filter(RoutingRule.account_id.is_(None))
            existing = query.first()
            if existing:
                existing.keywords = item.get("keywords", "")
                existing.target_name = item.get("target_name", "")
                existing.account_id = account_id
                existing.smtp_host = item.get("smtp_host", "")
                existing.smtp_port = item.get("smtp_port", 587)
                existing.smtp_username = item.get("smtp_username", "")
                existing.smtp_password_encrypted = item.get("smtp_password_encrypted", "")
                existing.priority = item.get("priority", 0)
                existing.enabled = item.get("enabled", True)
            else:
                rule = RoutingRule(
                    doc_type=doc_type,
                    keywords=item.get("keywords", ""),
                    target_email=target_email,
                    target_name=item.get("target_name", ""),
                    account_id=account_id,
                    smtp_host=item.get("smtp_host", ""),
                    smtp_port=item.get("smtp_port", 587),
                    smtp_username=item.get("smtp_username", ""),
                    smtp_password_encrypted=item.get("smtp_password_encrypted", ""),
                    priority=item.get("priority", 0),
                    enabled=item.get("enabled", True),
                )
                db.add(rule)
            stats["rules"] += 1

        # 导入系统设置 — 按 key 匹配，存在则更新，不存在则创建
        for item in data.get("system_settings", []):
            key = item["key"]
            value = item.get("value", "")
            existing = db.query(DefaultConfig).filter_by(key=key).first()
            if existing:
                existing.value = value
            else:
                db.add(DefaultConfig(key=key, value=value))
            stats["settings"] += 1

        db_retry_commit(db)

        flash(
            request,
            f"配置恢复成功！邮箱 {stats['accounts']} 个、LLM {stats['llm']} 个、"
            f"OCR {stats['ocr']} 个、规则 {stats['rules']} 条、设置 {stats['settings']} 项",
            "success",
        )

    except Exception as e:
        db.rollback()
        logger.error(f"配置恢复失败: {e}", exc_info=True)
        flash(request, f"配置恢复失败: {str(e)[:100]}", "error")

    return RedirectResponse(url="/backup", status_code=303)
