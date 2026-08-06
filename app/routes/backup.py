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
from app.models import EmailAccount, LLMConfig, OCRConfig, RoutingRule, DefaultConfig, DocTemplate
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
        "version": 2,
        "exported_at": datetime.now().isoformat(),
        "system_name": "文书分发系统",
        "email_accounts": [],
        "llm_config": [],
        "ocr_config": [],
        "routing_rules": [],
        "doc_templates": [],  # v2 新增：文书模板
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
            "model_type_locked": bool(cfg.model_type_locked),
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
            "connectivity_ok": cfg.connectivity_ok,
            "pdf_capable": cfg.pdf_capable,
        })

    # 路由规则
    for rule in db.query(RoutingRule).order_by(RoutingRule.id).all():
        # 查找账户名列表
        account_names = []
        if rule.account_ids and rule.account_ids.strip():
            ids = [int(x) for x in rule.account_ids.split(",") if x.strip().isdigit()]
            for aid in ids:
                acc = db.query(EmailAccount).filter_by(id=aid).first()
                if acc:
                    account_names.append(acc.name)
        data["routing_rules"].append({
            "doc_type": rule.doc_type,
            "keywords": rule.keywords,
            "target_email": rule.target_email,
            "target_name": rule.target_name,
            "account_names": account_names,
            "smtp_host": rule.smtp_host,
            "smtp_port": rule.smtp_port,
            "smtp_username": rule.smtp_username,
            "smtp_password_encrypted": rule.smtp_password_encrypted,
            "priority": rule.priority,
            "enabled": rule.enabled,
        })

    # 文书模板
    for tpl in db.query(DocTemplate).order_by(DocTemplate.id).all():
        account_name = None
        if tpl.account_id:
            acc = db.query(EmailAccount).filter_by(id=tpl.account_id).first()
            if acc:
                account_name = acc.name
        data["doc_templates"].append({
            "account_name": account_name,  # null = 全局模板
            "name": tpl.name,
            "doc_type": tpl.doc_type,
            "content": tpl.content,
            "description": tpl.description,
            "is_default": tpl.is_default,
        })

    # 系统设置（classify_llm_config_id 用名称代替脆弱的数字 ID）
    for cfg in db.query(DefaultConfig).order_by(DefaultConfig.key).all():
        value = cfg.value
        if cfg.key == "classify_llm_config_id" and value and value.isdigit():
            llm = db.query(LLMConfig).filter_by(id=int(value)).first()
            if llm:
                value = f"__name__:{llm.name}"
        data["system_settings"].append({
            "key": cfg.key,
            "value": value,
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

    stats = {"accounts": 0, "llm": 0, "ocr": 0, "rules": 0, "templates": 0, "settings": 0}

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
                existing.model_type_locked = bool(item.get("model_type_locked", False))
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
                    model_type_locked=bool(item.get("model_type_locked", False)),
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
                # 恢复测试结果（仅导入 JSON 中有值时才覆盖，防止旧备份清空检测结果）
                if "connectivity_ok" in item:
                    existing.connectivity_ok = item["connectivity_ok"]
                if "pdf_capable" in item:
                    existing.pdf_capable = item["pdf_capable"]
            else:
                cfg = OCRConfig(
                    name=ocr_name,
                    provider_type=item.get("provider_type", "paddleocr"),
                    api_url=item["api_url"],
                    api_key_encrypted=item.get("api_key_encrypted", ""),
                    model_name=item.get("model_name", ""),
                    is_active=item.get("is_active", True),
                    connectivity_ok=item.get("connectivity_ok"),
                    pdf_capable=item.get("pdf_capable"),
                )
                db.add(cfg)
            stats["ocr"] += 1

        # 导入路由规则 — 按 (doc_type, target_email) 匹配，存在则更新，不存在则创建
        for item in data.get("routing_rules", []):
            # 从 account_names（列表）或 account_name（旧格式兼容）构建 account_ids
            account_ids_str = ""
            names = item.get("account_names") or []
            old_name = item.get("account_name")
            if old_name and not names:
                names = [old_name]
            if names:
                mapped_ids = [str(account_map[n]) for n in names if n in account_map]
                account_ids_str = ",".join(mapped_ids)

            doc_type = item["doc_type"]
            target_email = item["target_email"]
            # 匹配已有规则：同文书类型 + 同目标邮箱
            existing = db.query(RoutingRule).filter_by(
                doc_type=doc_type, target_email=target_email
            ).first()
            if existing:
                existing.keywords = item.get("keywords", "")
                existing.target_name = item.get("target_name", "")
                existing.account_ids = account_ids_str
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
                    account_ids=account_ids_str,
                    smtp_host=item.get("smtp_host", ""),
                    smtp_port=item.get("smtp_port", 587),
                    smtp_username=item.get("smtp_username", ""),
                    smtp_password_encrypted=item.get("smtp_password_encrypted", ""),
                    priority=item.get("priority", 0),
                    enabled=item.get("enabled", True),
                )
                db.add(rule)
            stats["rules"] += 1

        # 导入文书模板 — 按 (account_name, name) 匹配，存在则更新，不存在则创建
        for item in data.get("doc_templates", []):
            account_id = None
            account_name = item.get("account_name")
            if account_name and account_name in account_map:
                account_id = account_map[account_name]

            # 匹配已有模板：同账户（或全局） + 同模板名称
            existing = None
            if account_id is not None:
                existing = db.query(DocTemplate).filter_by(
                    account_id=account_id, name=item["name"]
                ).first()
            else:
                existing = db.query(DocTemplate).filter_by(
                    account_id=None, name=item["name"]
                ).first()

            if existing:
                existing.doc_type = item.get("doc_type", "")
                existing.content = item.get("content", "")
                existing.description = item.get("description", "")
                existing.is_default = item.get("is_default", False)
            else:
                tpl = DocTemplate(
                    account_id=account_id,
                    name=item["name"],
                    doc_type=item.get("doc_type", ""),
                    content=item.get("content", ""),
                    description=item.get("description", ""),
                    is_default=item.get("is_default", False),
                )
                db.add(tpl)
            stats["templates"] += 1

        # 导入系统设置 — 按 key 匹配，存在则更新，不存在则创建
        # 处理 classify_llm_config_id：将 __name__:xxx 解析回当前数据库中的 ID
        llm_name_to_id = {}
        for llm in db.query(LLMConfig).all():
            llm_name_to_id[llm.name] = llm.id

        for item in data.get("system_settings", []):
            key = item["key"]
            value = item.get("value", "")
            # 解析 classify_llm_config_id 的名称引用
            if key == "classify_llm_config_id" and value.startswith("__name__:"):
                llm_name = value[9:]
                resolved_id = llm_name_to_id.get(llm_name, "")
                value = str(resolved_id) if resolved_id else ""
            existing = db.query(DefaultConfig).filter_by(key=key).first()
            if existing:
                existing.value = value
            else:
                db.add(DefaultConfig(key=key, value=value))
            stats["settings"] += 1

        db_retry_commit(db)

        parts = [
            f"邮箱 {stats['accounts']} 个",
            f"LLM {stats['llm']} 个",
            f"OCR {stats['ocr']} 个",
            f"规则 {stats['rules']} 条",
            f"模板 {stats['templates']} 个",
            f"设置 {stats['settings']} 项",
        ]
        flash(request, "配置恢复成功！" + "、".join(parts), "success")

    except Exception as e:
        db.rollback()
        logger.error(f"配置恢复失败: {e}", exc_info=True)
        flash(request, f"配置恢复失败: {str(e)[:100]}", "error")

    return RedirectResponse(url="/backup", status_code=303)
