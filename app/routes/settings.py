"""
系统设置路由 — 基本参数配置
"""
import logging
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import DefaultConfig, EmailAccount
from app.flash import flash
from app.csrf import check_csrf
from app.config import set_system_name, SYSTEM_PORT
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["系统设置"])

# 可配置的参数键及其默认值
SETTING_DEFAULTS = {
    "system_name": "文书分发系统",
    "system_port": "8888",
    "default_check_interval": "30",
    "monitor_days": "7",
    "log_retention_days": "90",  # 默认 90 天；设为 0 表示永久保留（不推荐）
    "llm_retry_interval": "10",  # LLM 分析重试间隔（秒）
    "llm_max_retries": "3",      # LLM 分析最大重试次数
    "analysis_output_mode": "content",  # AI解读输出模式: content=邮件正文, attachment=Word附件
    "revision_enabled": "false",        # 是否生成修改版文书
    "revision_prompt": "",              # 自定义修订提示词(空=使用默认)
    "revision_highlight": "true",       # 色彩标注改动（蓝色新增/红色修改/删除线建议删除）
    "context_window_tokens": "0",       # 上下文窗口大小(0=自动探测)
    "review_template_enabled": "false", # 启用审核意见模板
    "review_template_path": "templates/合同审核意见模板.docx",  # 模板文件路径
    "admin_email": "",              # 日报接收邮箱（空=不发送）
    "daily_report_enabled": "true", # 日报开关
    "daily_report_time": "09:00",   # 日报发送时间 (HH:MM, 24小时制)
}


def _get_setting(db: Session, key: str) -> str:
    """读取单个设置值，没有则返回默认值"""
    cfg = db.query(DefaultConfig).filter_by(key=key).first()
    if cfg and cfg.value:
        return cfg.value
    return SETTING_DEFAULTS.get(key, "")


def _get_all_settings(db: Session) -> dict:
    """读取所有系统设置"""
    return {key: _get_setting(db, key) for key in SETTING_DEFAULTS}


def _save_setting(db: Session, key: str, value: str):
    """保存单个设置"""
    cfg = db.query(DefaultConfig).filter_by(key=key).first()
    if cfg:
        cfg.value = value
    else:
        db.add(DefaultConfig(key=key, value=value))


@router.get("")
async def settings_page(request: Request, db: Session = Depends(get_db)):
    """系统设置页面"""
    from app.scheduler import scheduler

    settings = _get_all_settings(db)
    accounts = db.query(EmailAccount).filter_by(enabled=True).all()

    return request.app.state.templates.TemplateResponse(request, "settings.html", {
        "request": request,
        "active_page": "settings",
        "settings": settings,
        "scheduler_running": scheduler.running,
        "account_count": len(accounts),
    })


@router.post("/save")
async def save_settings(
    request: Request,
    db: Session = Depends(get_db),
    system_name: str = Form("文书分发系统"),
    system_port: str = Form("8888"),
    default_check_interval: int = Form(30),
    monitor_days: int = Form(7),
    log_retention_days: int = Form(0),
    llm_retry_interval: int = Form(10),
    llm_max_retries: int = Form(3),
    analysis_output_mode: str = Form("content"),
    revision_enabled: str = Form("false"),
    revision_prompt: str = Form(""),
    revision_highlight: str = Form("true"),
    context_window_tokens: str = Form("0"),
    review_template_enabled: str = Form("false"),
    review_template_path: str = Form(""),
    admin_email: str = Form(""),
    daily_report_enabled: str = Form("true"),
    daily_report_time: str = Form("09:00"),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """保存所有系统设置"""
    check_csrf(request, form_csrf)
    # 保存各项设置
    _save_setting(db, "system_name", system_name.strip())
    _save_setting(db, "system_port", system_port.strip())
    _save_setting(db, "default_check_interval", str(default_check_interval))
    _save_setting(db, "monitor_days", str(monitor_days))
    _save_setting(db, "log_retention_days", str(log_retention_days))
    _save_setting(db, "llm_retry_interval", str(llm_retry_interval))
    _save_setting(db, "llm_max_retries", str(llm_max_retries))
    _save_setting(db, "analysis_output_mode", analysis_output_mode)
    _save_setting(db, "revision_enabled", "true" if revision_enabled.lower() in ("true", "on", "1") else "false")
    _save_setting(db, "revision_prompt", revision_prompt)
    _save_setting(db, "revision_highlight", "true" if revision_highlight.lower() in ("true", "on", "1") else "false")
    _save_setting(db, "context_window_tokens", context_window_tokens.strip())
    _save_setting(db, "review_template_enabled", "true" if review_template_enabled.lower() in ("true", "on", "1") else "false")
    _save_setting(db, "review_template_path", review_template_path.strip())
    _save_setting(db, "admin_email", admin_email.strip())
    _save_setting(db, "daily_report_enabled", "true" if daily_report_enabled.lower() in ("true", "on", "1") else "false")
    _save_setting(db, "daily_report_time", daily_report_time.strip())
    db.commit()

    # 更新全局缓存
    set_system_name(system_name.strip())
    # Jinja2 全局变量需要显式更新（字符串是不可变对象）
    request.app.state.templates.env.globals["system_name"] = system_name.strip()

    # ⚠️ 不再覆盖已有账户的独立检查间隔
    # 系统默认间隔仅影响新建账户的初始值，不影响已配置账户

    # 判断端口是否变更
    port_changed = system_port.strip() != SYSTEM_PORT
    if port_changed:
        flash(
            request,
            f"设置已保存。⚠️ 端口已改为 {system_port}，需要手动重启服务才能生效",
            "warning",
        )
    else:
        flash(request, "系统设置已保存", "success")

    return RedirectResponse(url="/settings", status_code=303)


@router.post("/restart")
async def restart_service(request: Request, form_csrf: str = Form("", alias="_csrf_token"), db: Session = Depends(get_db)):
    """重启服务（根据当前端口设置）"""
    check_csrf(request, form_csrf)
    import threading
    import time
    import os
    import sys

    port = _get_setting(db, "system_port") or "8888"

    def do_restart():
        time.sleep(0.5)  # 等待 HTTP 响应发送完成
        # os.execv 原子替换当前进程为新 uvicorn，无端口冲突，无竞态
        os.execv(
            sys.executable,
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "0.0.0.0", "--port", port],
        )

    thread = threading.Thread(target=do_restart, daemon=True)
    thread.start()

    return {"success": True, "message": f"服务正在重启，新端口: {port}，请稍后刷新页面"}


@router.post("/shutdown")
async def shutdown_service(request: Request, form_csrf: str = Form("", alias="_csrf_token")):
    """停止服务"""
    check_csrf(request, form_csrf)
    import threading
    import time
    import os

    def do_shutdown():
        time.sleep(1.0)  # 等待 HTTP 响应发送完成
        os._exit(0)  # 强制退出当前进程，OS 会自动释放端口

    thread = threading.Thread(target=do_shutdown, daemon=True)
    thread.start()

    return {"success": True, "message": "服务正在关闭..."}


@router.get("/detect-context-window")
async def detect_context_window_endpoint(request: Request, db: Session = Depends(get_db)):
    """探测当前 LLM 模型的上下文窗口大小"""
    from app.models import LLMConfig
    from app.config import decrypt
    from app.llm_analyzer import detect_context_window

    llm = db.query(LLMConfig).filter_by(is_active=True).first()
    if not llm:
        return {"success": False, "message": "未找到激活的 LLM 配置"}

    try:
        api_key = decrypt(llm.api_key_encrypted) if llm.api_key_encrypted else ""
        window = detect_context_window(llm.api_url, api_key, llm.model_name)
        return {
            "success": True,
            "window": window,
            "model": llm.model_name,
            "message": f"探测完成：{llm.model_name} 上下文窗口 = {window:,} tokens",
        }
    except Exception as e:
        logger.error(f"探测上下文窗口失败: {e}")
        return {"success": False, "message": f"探测失败: {e}"}


