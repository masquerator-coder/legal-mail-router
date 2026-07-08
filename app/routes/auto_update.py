"""
自动更新路由 — 检查和应用 git 远程更新
"""
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.routes.settings import _get_setting, _save_setting, SETTING_DEFAULTS
from app.csrf import check_csrf
from app.config import BASE_DIR, VERSION

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["自动更新"])

# Git 操作超时
_GIT_TIMEOUT = 30


def _git(*args: str, timeout: int = _GIT_TIMEOUT) -> tuple[int, str, str]:
    """执行 git 命令，返回 (returncode, stdout, stderr)"""
    try:
        result = subprocess.run(
            ["git"] + list(args),
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(BASE_DIR),
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return -1, "", "git 命令不可用，请确认已安装 git"
    except subprocess.TimeoutExpired:
        return -2, "", f"git 操作超时（{timeout}秒）"
    except Exception as e:
        return -3, "", str(e)


def _is_git_repo() -> bool:
    """检查当前目录是否是 git 仓库"""
    rc, _, _ = _git("rev-parse", "--git-dir")
    return rc == 0


def get_current_commit() -> str:
    """获取当前 HEAD 的短 commit hash"""
    rc, out, _ = _git("rev-parse", "--short", "HEAD")
    return out if rc == 0 else ""


def get_current_branch() -> str:
    """获取当前分支名"""
    rc, out, _ = _git("rev-parse", "--abbrev-ref", "HEAD")
    return out if rc == 0 else ""


def get_remote_status(remote: str = "gitcode", branch: str = "main") -> dict:
    """获取远程仓库状态，返回 {behind, ahead, remote_commit, remote_commit_short, error}"""
    result = {
        "behind": 0,
        "ahead": 0,
        "remote_commit": "",
        "remote_commit_short": "",
        "error": "",
    }

    # 添加 remote（如果不存在）
    rc, _, _ = _git("remote", "get-url", remote)
    if rc != 0:
        result["error"] = f"remote '{remote}' 未配置"
        return result

    # 获取最新
    rc, _, stderr = _git("fetch", remote, branch)
    if rc != 0:
        result["error"] = f"获取远程失败: {stderr[:200]}"
        return result

    # 比较 commit
    rc, out, _ = _git("rev-parse", f"{remote}/{branch}")
    if rc != 0:
        result["error"] = f"无法获取 {remote}/{branch}"
        return result
    result["remote_commit"] = out

    rc, out, _ = _git("rev-parse", "--short", f"{remote}/{branch}")
    result["remote_commit_short"] = out if rc == 0 else ""

    # 计算 behind/ahead
    rc, out, _ = _git("rev-list", "--count", f"HEAD..{remote}/{branch}")
    if rc == 0:
        result["behind"] = int(out or "0")

    rc, out, _ = _git("rev-list", "--count", f"{remote}/{branch}..HEAD")
    if rc == 0:
        result["ahead"] = int(out or "0")

    return result


def git_pull(remote: str = "gitcode", branch: str = "main") -> dict:
    """执行 git pull，返回 {success, message, output}"""
    rc, out, stderr = _git("pull", "--ff-only", remote, branch)
    if rc == 0:
        new_commit = get_current_commit()
        return {"success": True, "message": f"更新成功，当前 commit: {new_commit}", "output": out}
    else:
        return {"success": False, "message": f"拉取失败: {stderr[:300]}", "output": stderr}


# ── API ──

@router.get("/update/status")
async def update_status(request: Request, db: Session = Depends(get_db)):
    """获取更新状态 JSON"""
    is_git = _is_git_repo()
    commit = get_current_commit() if is_git else ""
    branch = get_current_branch() if is_git else ""

    remote_status = get_remote_status() if is_git else {}

    auto_enabled = _get_setting(db, "auto_update_enabled") == "true"
    last_check = _get_setting(db, "auto_update_last_check") or ""

    return {
        "version": VERSION,
        "is_git_repo": is_git,
        "current_commit": commit,
        "current_branch": branch,
        "remote": remote_status,
        "auto_enabled": auto_enabled,
        "last_check": last_check,
        "update_available": remote_status.get("behind", 0) > 0,
    }


@router.post("/update/check")
async def check_update(request: Request, db: Session = Depends(get_db),
                       form_csrf: str = Form("", alias="_csrf_token")):
    """检查远程更新"""
    check_csrf(request, form_csrf)

    if not _is_git_repo():
        return {"success": False, "message": "当前目录不是 git 仓库"}

    remote_status = get_remote_status()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    _save_setting(db, "auto_update_last_check", now)

    if remote_status.get("error"):
        return {"success": False, "message": remote_status["error"]}

    behind = remote_status.get("behind", 0)
    if behind > 0:
        return {
            "success": True,
            "update_available": True,
            "behind": behind,
            "remote_commit": remote_status.get("remote_commit_short", ""),
            "message": f"发现 {behind} 个新提交（远程: {remote_status.get('remote_commit_short', '?')}）",
        }
    else:
        return {
            "success": True,
            "update_available": False,
            "message": "已是最新版本",
        }


@router.post("/update/apply")
async def apply_update(request: Request, form_csrf: str = Form("", alias="_csrf_token")):
    """拉取远程更新并重启服务"""
    check_csrf(request, form_csrf)

    if not _is_git_repo():
        return JSONResponse(
            {"success": False, "message": "当前目录不是 git 仓库"},
            status_code=400,
        )

    # 先 fetch 确保最新
    remote_status = get_remote_status()
    if remote_status.get("error"):
        return JSONResponse(
            {"success": False, "message": remote_status["error"]},
            status_code=400,
        )

    if remote_status.get("behind", 0) == 0:
        # 没有更新，继续 pull 以处理 fast-forward
        pass

    # 执行 git pull
    result = git_pull()
    if not result["success"]:
        return JSONResponse(
            {"success": False, "message": result["message"]},
            status_code=500,
        )

    # 更新后重启服务
    def _do_restart():
        time.sleep(1.0)
        if sys.platform == "win32":
            os.kill(os.getpid(), signal.CTRL_BREAK_EVENT)
        else:
            os.kill(os.getpid(), signal.SIGTERM)

    import threading
    thread = threading.Thread(target=_do_restart, daemon=True)
    thread.start()

    return {"success": True, "message": result["message"] + " 服务即将重启..."}


# ── 定时检查集成（在 scheduler 中调用） ──

def check_and_apply_auto_update(db: Session) -> dict | None:
    """由 scheduler 定时调用：检查自动更新配置并执行"""
    enabled = _get_setting(db, "auto_update_enabled") == "true"
    if not enabled:
        return None

    if not _is_git_repo():
        return None

    branch = _get_setting(db, "auto_update_branch") or "main"
    remote = "gitcode"  # 默认 remote

    remote_status = get_remote_status(remote, branch)
    if remote_status.get("error") or remote_status.get("behind", 0) == 0:
        return remote_status  # 无更新或出错

    # 有更新 → 自动拉取
    result = git_pull(remote, branch)
    logger.info(
        "自动更新: %s, behind=%s, commit=%s",
        "成功" if result["success"] else "失败",
        remote_status.get("behind", 0),
        get_current_commit(),
    )
    return result
