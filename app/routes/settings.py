"""
系统设置路由 — 基本参数配置
"""
import logging
import signal
import sys
from pathlib import Path
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from app.database import get_db, db_retry_commit
from app.models import DefaultConfig, EmailAccount
from app.flash import flash
from app.csrf import check_csrf
from app.config import set_system_name, set_system_port, SYSTEM_PORT
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["系统设置"])

# 可配置的参数键及其默认值
SETTING_DEFAULTS = {
    "system_name": "文书分发系统",
    "system_port": "8020",
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
    # ── 法律知识库（LLM Wiki）──
    "kb_enabled": "false",           # 启用知识库检索
    "kb_api_base": "http://127.0.0.1:19828",  # LLM Wiki API 地址
    "kb_token": "",                  # API Token（在 LLM Wiki 设置页生成）
    "kb_project_id": "",             # 知识库项目 ID
    "kb_search_max_chars": "5000",   # 知识库检索结果最大字符数（0=不截断）
    "email_body_max_chars": "8000",  # 送入 LLM 的邮件正文最大字符数（0=不截断）
    "llm_timeout": "180",            # LLM 调用超时（秒），含分析 + 修改版文书
    "context_window_usage_ratio": "0.50",  # 输入占上下文窗口的比例（0.1-0.95）
    "token_estimation_method": "approximate",  # token估算方法: approximate / tiktoken
    # ── 自动更新 ──
    "auto_update_enabled": "false",          # 启用自动更新
    "auto_update_branch": "main",            # 跟踪分支
    "auto_update_interval_hours": "6",       # 检查间隔（小时）
    # ── 多附件分组分析 ──
    "attachment_grouping": "false",           # 启用多附件分组分析
    "classify_use_main_llm": "true",          # 预分类使用主LLM(true)/指定LLM(false)
    "classify_llm_config_id": "",             # 预分类专用LLM配置ID
    # ── 全局发件人黑名单 ──
    "global_sender_blacklist": "",            # 全局排除地址（逗号分隔，所有邮箱共用）
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

    import os as _os
    is_docker = _os.path.exists("/.dockerenv") or _os.environ.get("DOCKER_CONTAINER", "")
    docker_port = _os.environ.get("PORT", "") if is_docker else ""

    from app.models import LLMConfig
    llm_configs = db.query(LLMConfig).order_by(LLMConfig.id).all()

    return request.app.state.templates.TemplateResponse(request, "settings.html", {
        "request": request,
        "active_page": "settings",
        "settings": settings,
        "scheduler_running": scheduler.running,
        "account_count": len(accounts),
        "is_docker": is_docker,
        "docker_port": docker_port,
        "llm_configs": llm_configs,
    })


@router.post("/save")
async def save_settings(
    request: Request,
    db: Session = Depends(get_db),
    system_name: str = Form("文书分发系统"),
    system_port: str = Form("8020"),
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
    # ── 法律知识库 ──
    kb_enabled: str = Form("false"),
    kb_api_base: str = Form("http://127.0.0.1:19828"),
    kb_token: str = Form(""),
    kb_project_id: str = Form(""),
    kb_search_max_chars: str = Form("5000"),
    email_body_max_chars: str = Form("8000"),
    llm_timeout: int = Form(180),
    context_window_usage_ratio: str = Form("0.50"),
    token_estimation_method: str = Form("approximate"),
    # ── 多附件分组分析 ──
    attachment_grouping: str = Form("false"),
    classify_use_main_llm: str = Form("true"),
    classify_llm_config_id: str = Form(""),
    # ── 自动更新 ──
    auto_update_enabled: str = Form("false"),
    auto_update_branch: str = Form("main"),
    auto_update_interval_hours: str = Form("6"),
    # ── 全局发件人黑名单 ──
    global_sender_blacklist: str = Form(""),
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
    # ── 法律知识库 ──
    _save_setting(db, "kb_enabled", "true" if kb_enabled.lower() in ("true", "on", "1") else "false")
    _save_setting(db, "kb_api_base", kb_api_base.strip())
    _save_setting(db, "kb_token", kb_token.strip())
    _save_setting(db, "kb_project_id", kb_project_id.strip())
    _save_setting(db, "kb_search_max_chars", kb_search_max_chars.strip())
    _save_setting(db, "email_body_max_chars", email_body_max_chars.strip())
    _save_setting(db, "llm_timeout", str(llm_timeout))
    _save_setting(db, "context_window_usage_ratio", context_window_usage_ratio.strip())
    _save_setting(db, "token_estimation_method", token_estimation_method.strip())
    # ── 多附件分组分析 ──
    _save_setting(db, "attachment_grouping", "true" if attachment_grouping.lower() in ("true", "on", "1") else "false")
    _save_setting(db, "classify_use_main_llm", "true" if classify_use_main_llm.lower() in ("true", "on", "1") else "false")
    _save_setting(db, "classify_llm_config_id", classify_llm_config_id.strip())
    # ── 自动更新 ──
    _save_setting(db, "auto_update_enabled", "true" if auto_update_enabled.lower() in ("true", "on", "1") else "false")
    _save_setting(db, "auto_update_branch", auto_update_branch.strip() or "main")
    _save_setting(db, "auto_update_interval_hours", auto_update_interval_hours.strip() or "6")
    # ── 全局发件人黑名单 ──
    _save_setting(db, "global_sender_blacklist", global_sender_blacklist.strip())
    db_retry_commit(db)

    # 更新全局缓存
    set_system_name(system_name.strip())
    set_system_port(system_port.strip())
    # Jinja2 全局变量需要显式更新（字符串是不可变对象）
    request.app.state.templates.env.globals["system_name"] = system_name.strip()

    # ⚠️ 不再覆盖已有账户的独立检查间隔
    # 系统默认间隔仅影响新建账户的初始值，不影响已配置账户

    # 判断端口是否变更
    port_changed = system_port.strip() != SYSTEM_PORT
    if port_changed:
        import os as _os
        is_docker = _os.path.exists("/.dockerenv") or _os.environ.get("DOCKER_CONTAINER", "")
        if is_docker:
            docker_port = _os.environ.get("PORT", "8020")
            flash(
                request,
                f"设置已保存。⚠️ Docker 环境下端口由 docker-compose.yml 控制（当前容器端口: {docker_port}）。"
                f"修改端口需同步更新 docker-compose.yml 的 ports 映射和 PORT 环境变量后重建容器。",
                "warning",
            )
        else:
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
    """重启服务"""
    check_csrf(request, form_csrf)
    import threading
    import time
    import os
    import sys
    import signal
    import subprocess

    # ── Docker 环境检测 ──
    is_docker = os.path.exists("/.dockerenv") or os.environ.get("DOCKER_CONTAINER", "")
    docker_port = os.environ.get("PORT", "8020")
    db_port = _get_setting(db, "system_port") or "8020"

    if is_docker:
        port = docker_port
        if db_port != docker_port:
            msg = (
                f"⚠️ 数据库端口 ({db_port}) 与容器端口 ({docker_port}) 不一致。"
                f"容器内将使用 {docker_port} 端口重启。"
                f"如需修改端口，请更新 docker-compose.yml 的 ports 映射和 PORT 环境变量后重建容器。"
            )
        else:
            msg = f"服务正在重启，端口: {port}，请稍后刷新页面"
    else:
        port = db_port
        msg = f"服务正在重启，新端口: {port}，请稍后刷新页面"

    def do_restart():
        time.sleep(1.0)  # 等待 HTTP 响应发送完成
        # 生成延迟启动的辅助进程：先等当前进程退出释放端口，再启动 uvicorn
        if sys.platform == "win32":
            # Windows: 使用 ping 延迟 + 启动
            cmd = f'ping 127.0.0.1 -n 3 > nul && {sys.executable} -m uvicorn app.main:app --host 0.0.0.0 --port {port}'
            subprocess.Popen(
                ["cmd.exe", "/c", cmd],
                close_fds=True,
                cwd=str(Path(__file__).resolve().parent.parent.parent),
            )
        else:
            # Linux/Mac: 使用 sleep + exec 方式
            startup = f'sleep 2; exec {sys.executable} -m uvicorn app.main:app --host 0.0.0.0 --port {port}'
            subprocess.Popen(
                ["/bin/sh", "-c", startup],
                close_fds=True,
                cwd=str(Path(__file__).resolve().parent.parent.parent),
            )
        # 向自身发送 SIGTERM 触发 uvicorn 优雅关闭（lifespan shutdown 会执行）
        if sys.platform == "win32":
            os.kill(os.getpid(), signal.CTRL_BREAK_EVENT)
        else:
            os.kill(os.getpid(), signal.SIGTERM)

    thread = threading.Thread(target=do_restart, daemon=True)
    thread.start()

    return {"success": True, "message": msg}


@router.post("/shutdown")
async def shutdown_service(request: Request, form_csrf: str = Form("", alias="_csrf_token")):
    """停止服务（优雅关闭，触发 lifespan shutdown 清理资源）"""
    check_csrf(request, form_csrf)
    import threading
    import time
    import os
    import signal

    def do_shutdown():
        time.sleep(1.0)  # 等待 HTTP 响应发送完成
        # 向自身发送 SIGTERM，uvicorn 会捕获并执行 lifespan shutdown
        # 从而触发 scheduler.shutdown() 等清理逻辑
        if sys.platform == "win32":
            os.kill(os.getpid(), signal.CTRL_BREAK_EVENT)
        else:
            os.kill(os.getpid(), signal.SIGTERM)

    thread = threading.Thread(target=do_shutdown, daemon=True)
    thread.start()

    return {"success": True, "message": "服务正在优雅关闭..."}


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


@router.get("/kb-health")
async def kb_health_check(api_base: str = "http://127.0.0.1:19828"):
    """测试知识库连接（服务端代理，避免浏览器 CORS 限制）"""
    from app.kb_client import health_check
    try:
        ok = await health_check(api_base)
        if ok:
            return {"success": True, "message": "知识库服务连接成功"}
        else:
            return {"success": False, "message": "知识库服务不可达"}
    except Exception as e:
        return {"success": False, "message": f"连接失败: {e}"}


@router.get("/default-revision-prompt")
async def get_default_revision_prompt():
    """获取系统默认修订提示词（从 修订提示词.md 或代码回退）"""
    try:
        from app.llm_analyzer import _get_default_revision_prompt
        prompt = _get_default_revision_prompt()
        return {"prompt": prompt}
    except Exception as e:
        logger.error(f"获取默认修订提示词失败: [{type(e).__name__}] {e}")
        return {"prompt": "", "error": str(e)}


def get_kb_config(db: Session) -> dict:
    """读取法律知识库（LLM Wiki）配置，供 LLM 分析流程使用"""
    return {
        "enabled": _get_setting(db, "kb_enabled") == "true",
        "api_base": _get_setting(db, "kb_api_base").strip() or "http://127.0.0.1:19828",
        "token": _get_setting(db, "kb_token").strip(),
        "project_id": _get_setting(db, "kb_project_id").strip(),
    }


