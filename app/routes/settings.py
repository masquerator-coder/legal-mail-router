"""
系统设置路由 — 基本参数配置
"""
import json
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


def _is_valid_port(value: str) -> bool:
    """端口必须为 1-65535 的纯数字（防止拼接进 shell 命令执行）"""
    import re as _re
    if not _re.fullmatch(r"\d{1,5}", value):
        return False
    try:
        return 1 <= int(value) <= 65535
    except ValueError:
        return False

# 北大法宝 MCP 服务器默认配置（Token 用占位符 __PKULAW_TOKEN__，首次使用时在设置页替换为真实 Token）
_MCP_DEFAULT_TOKEN = "__PKULAW_TOKEN__"

_PKULAW_MCP_SERVERS = {
    "pkulaw-law-search": "https://apim-gateway.pkulaw.com/mcp-law-search-service",
    "pkulaw-law-keyword": "https://apim-gateway.pkulaw.com/mcp-law",
    "pkulaw-case-semantic-search": "https://apim-gateway.pkulaw.com/mcp-case-search-service",
    "pkulaw-case-keyword": "https://apim-gateway.pkulaw.com/mcp-case",
    "pkulaw-law-item-keyword": "https://apim-gateway.pkulaw.com/mcp-fatiao",
    "pkulaw-law-recognition": "https://apim-gateway.pkulaw.com/law_recognition",
    "pkulaw-case-number-recognition": "https://apim-gateway.pkulaw.com/case_number_recognition",
    "pkulaw-citation-validator": "https://apim-gateway.pkulaw.com/pku_citation_validator",
    "pkulaw-doc-link": "https://apim-gateway.pkulaw.com/add-doc-link",
}

DEFAULT_MCP_SERVERS = json.dumps(
    {
        "mcpServers": {
            name: {
                "type": "streamableHttp",
                "url": url,
                "headers": {"Authorization": f"Bearer {_MCP_DEFAULT_TOKEN}"},
            }
            for name, url in _PKULAW_MCP_SERVERS.items()
        }
    },
    ensure_ascii=False,
)

# 可配置的参数键及其默认值
SETTING_DEFAULTS = {
    "system_name": "邮件智能分析转发系统",
    "system_port": "8020",
    "default_check_interval": "30",
    "monitor_days": "7",
    "log_retention_days": "90",  # 默认 90 天；设为 0 表示永久保留（不推荐）
    "llm_retry_interval": "10",  # LLM 分析重试间隔（秒）
    "llm_max_retries": "3",      # LLM 分析最大重试次数
    "analysis_output_mode": "content",  # AI解读输出模式: content=邮件正文, attachment=Word附件（权威默认值，scheduler.py / logs.py / mail_forwarder.py 兜底需与此一致）
    "revision_enabled": "false",        # 是否生成修改版文书
    "revision_highlight": "true",       # 色彩标注改动（蓝色新增/红色修改/删除线建议删除）
    "context_window_tokens": "0",       # 上下文窗口大小(0=自动探测)
    "review_template_enabled": "false", # 启用审核意见模板
    "review_template_path": "templates/合同审核意见模板.docx",  # 模板文件路径
    "admin_email": "",              # 日报接收邮箱（空=不发送）
    "daily_report_enabled": "true", # 日报开关
    "daily_report_time": "09:00",   # 日报发送时间 (HH:MM, 24小时制)
    "email_body_max_chars": "0",  # 邮件正文上限（0=由上下文窗口自动推导）
    # ── MCP 工具（如北大法宝法规检索） ──
    "mcp_enabled": "false",        # 启用 MCP 工具（第二阶段文书分析时供 LLM 调用）
    "mcp_servers": DEFAULT_MCP_SERVERS,  # MCP 服务器配置 JSON（预填北大法宝，Token 占位符 __PKULAW_TOKEN__）
    "mcp_max_turns": "5",           # MCP 工具调用的最大轮次（模型需多轮检索后才输出最终 JSON，默认 5 较稳妥）
    "llm_timeout": "180",            # LLM 调用超时（秒），含分析 + 修改版文书
    "context_window_usage_ratio": "0.50",  # 输入占上下文窗口的比例（0.1-0.95）
    "token_estimation_method": "approximate",  # token估算方法: approximate / tiktoken
    # ── 自动更新 ──
    "auto_update_enabled": "false",          # 启用自动更新
    "auto_update_branch": "main",            # 跟踪分支
    "auto_update_interval_hours": "6",       # 检查间隔（小时）
    # ── 多附件分组分析 ──
    "attachment_grouping": "false",           # 启用多附件分组分析（分组模型在 LLM 配置页分配）
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
    from app.services.scheduler import scheduler

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
    system_name: str = Form("邮件智能分析转发系统"),
    system_port: str = Form("8020"),
    default_check_interval: int = Form(30),
    monitor_days: int = Form(7),
    log_retention_days: int = Form(0),
    llm_retry_interval: int = Form(10),
    llm_max_retries: int = Form(3),
    analysis_output_mode: str = Form("content"),
    revision_enabled: str = Form("false"),
    revision_highlight: str = Form("true"),
    context_window_tokens: str = Form("0"),
    review_template_enabled: str = Form("false"),
    review_template_path: str = Form(""),
    admin_email: str = Form(""),
    daily_report_enabled: str = Form("true"),
    daily_report_time: str = Form("09:00"),
    email_body_max_chars: str = Form("0"),
    mcp_enabled: str = Form("false"),
    mcp_servers: str = Form(""),
    mcp_max_turns: int = Form(5),
    llm_timeout: int = Form(180),
    context_window_usage_ratio: str = Form("0.50"),
    token_estimation_method: str = Form("approximate"),
    # ── 多附件分组分析 ──
    attachment_grouping: str = Form("false"),
    # ── 自动更新 ──
    auto_update_enabled: str = Form("false"),
    auto_update_branch: str = Form("main"),
    auto_update_interval_hours: str = Form("6"),
    form_csrf: str = Form("", alias="_csrf_token"),
):
    """保存所有系统设置（支持 AJAX 自动保存与表单提交两种方式）"""
    check_csrf(request, form_csrf)
    is_ajax = request.headers.get("X-Requested-With") == "fetch" or "application/json" in (request.headers.get("Accept") or "")
    system_port = system_port.strip()
    if not _is_valid_port(system_port):
        if is_ajax:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                {"success": False, "message": f"端口格式无效: {system_port!r}，必须是 1-65535 的纯数字"},
                status_code=400,
            )
        flash(request, f"端口格式无效: {system_port!r}，必须是 1-65535 的纯数字", "error")
        return RedirectResponse(url="/settings", status_code=303)
    # 保存各项设置
    _save_setting(db, "system_name", system_name.strip())
    _save_setting(db, "system_port", system_port)
    _save_setting(db, "default_check_interval", str(default_check_interval))
    _save_setting(db, "monitor_days", str(monitor_days))
    _save_setting(db, "log_retention_days", str(log_retention_days))
    _save_setting(db, "llm_retry_interval", str(llm_retry_interval))
    _save_setting(db, "llm_max_retries", str(llm_max_retries))
    _save_setting(db, "analysis_output_mode", analysis_output_mode)
    _save_setting(db, "revision_enabled", "true" if revision_enabled.lower() in ("true", "on", "1") else "false")
    _save_setting(db, "revision_highlight", "true" if revision_highlight.lower() in ("true", "on", "1") else "false")
    _save_setting(db, "context_window_tokens", context_window_tokens.strip())
    _save_setting(db, "review_template_enabled", "true" if review_template_enabled.lower() in ("true", "on", "1") else "false")
    _save_setting(db, "review_template_path", review_template_path.strip())
    _save_setting(db, "admin_email", admin_email.strip())
    _save_setting(db, "daily_report_enabled", "true" if daily_report_enabled.lower() in ("true", "on", "1") else "false")
    _save_setting(db, "daily_report_time", daily_report_time.strip())
    _save_setting(db, "email_body_max_chars", email_body_max_chars.strip())
    _save_setting(db, "mcp_enabled", "true" if mcp_enabled.lower() in ("true", "on", "1") else "false")
    _save_setting(db, "mcp_servers", mcp_servers.strip())
    _save_setting(db, "mcp_max_turns", str(mcp_max_turns))
    _save_setting(db, "llm_timeout", str(llm_timeout))
    _save_setting(db, "context_window_usage_ratio", context_window_usage_ratio.strip())
    _save_setting(db, "token_estimation_method", token_estimation_method.strip())
    # ── 多附件分组分析 ──
    _save_setting(db, "attachment_grouping", "true" if attachment_grouping.lower() in ("true", "on", "1") else "false")
    # ── 自动更新 ──
    _save_setting(db, "auto_update_enabled", "true" if auto_update_enabled.lower() in ("true", "on", "1") else "false")
    _save_setting(db, "auto_update_branch", auto_update_branch.strip() or "main")
    _save_setting(db, "auto_update_interval_hours", auto_update_interval_hours.strip() or "6")
    db_retry_commit(db)

    # 更新全局缓存
    set_system_name(system_name.strip())
    set_system_port(system_port.strip())
    # 同步模板全局（字符串为不可变值，需在设置保存后重新赋值）
    request.app.state.templates.env.globals["system_name"] = system_name.strip()

    # ⚠️ 不再覆盖已有账户的独立检查间隔
    # 系统默认间隔仅影响新建账户的初始值，不影响已配置账户

    # 判断端口是否变更
    port_changed = system_port.strip() != SYSTEM_PORT
    if port_changed:
        import os as _os
        is_docker = _os.path.exists("/.dockerenv") or _os.environ.get("DOCKER_CONTAINER", "")
        if is_docker:
            msg = (
                f"设置已保存。⚠️ Docker 环境下端口由 docker-compose.yml 控制（当前容器端口: {docker_port}）。"
                f"修改端口需同步更新 docker-compose.yml 的 ports 映射和 PORT 环境变量后重建容器。"
            )
        else:
            msg = f"设置已保存。⚠️ 端口已改为 {system_port}，需要手动重启服务才能生效"
    else:
        msg = "系统设置已保存"

    if is_ajax:
        return {
            "success": True,
            "message": msg,
            "port_changed": port_changed,
        }

    flash(request, msg, "warning" if port_changed else "success")
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
        # 防御: 数据库 system_port 若为脏数据则回退默认端口
        # （用新变量名，避免对内层函数闭包变量赋值触发 UnboundLocalError）
        safe_port = port if _is_valid_port(port) else "8020"
        # 参数列表形式启动 uvicorn（不使用 shell 拼接，杜绝命令注入）
        base_cmd = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", safe_port]
        time.sleep(2.0)  # 再等 2 秒，确保当前 uvicorn 退出释放端口
        if sys.platform == "win32":
            # Windows: 脱离当前进程组启动，避免 os._exit 连带杀死子进程
            subprocess.Popen(
                base_cmd,
                close_fds=True,
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                cwd=str(Path(__file__).resolve().parent.parent.parent),
            )
        else:
            # Linux/Mac: 新会话启动，脱离当前进程组
            subprocess.Popen(
                base_cmd,
                close_fds=True,
                start_new_session=True,
                cwd=str(Path(__file__).resolve().parent.parent.parent),
            )
        # 向自身发送 SIGTERM 触发 uvicorn 优雅关闭（lifespan shutdown 会执行）
        if sys.platform == "win32":
            # Windows: os.kill 会杀死整个进程组（包括刚启动的子进程）
            # 改用 os._exit 直接从当前进程退出，不影响已 DETACHED 的子进程
            import os as _os
            _os._exit(0)
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
    from app.services.llm_analyzer import detect_context_window
    from app.services.scheduler import _get_role_llm_cfg

    # 探测文书解读模型（第二阶段）的上下文窗口
    llm = _get_role_llm_cfg(db, "analyzer")
    if not llm:
        return {"success": False, "message": "未找到文书解读模型配置，请在 LLM 配置页分配角色"}

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


@router.get("/mcp-tools")
async def mcp_list_tools(config: str = "", db: Session = Depends(get_db)):
    """测试 MCP 连接并列出可用工具（只读，不保存）。用于设置页「列出工具」按钮。

    config: 可选，当前设置页文本域里的 MCP 配置 JSON（测试未保存的改动）；
            为空时回退到数据库已保存的配置。

    注意：通过 ``asyncio.to_thread(list_mcp_tools, ...)`` 在独立线程 + 独立事件
    循环里执行 —— mcp SDK 的 anyio 任务组与 Starlette BaseHTTPMiddleware
    （CSRF 中间件）存在已知冲突，直接在端点里跑会返回 HTTP 500。
    """
    import asyncio

    from app.services.mcp_client import list_mcp_tools, parse_server_configs

    raw_cfg = config.strip() if config and config.strip() else (_get_setting(db, "mcp_servers") or "")
    servers = parse_server_configs(raw_cfg)
    if not servers:
        return {
            "success": False,
            "message": "未配置有效的 MCP 服务器（配置为空或 JSON 无效）",
            "tools": [],
        }
    return await asyncio.to_thread(list_mcp_tools, servers)



