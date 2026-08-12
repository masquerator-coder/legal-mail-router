#!/usr/bin/env python3
"""
邮件智能分析转发系统 — 启动与管理脚本

用法:
  python run.py                    # 前台启动（默认）
  python run.py start              # 前台启动
  python run.py start --daemon     # 后台启动（守护进程）
  python run.py install            # 安装依赖
  python run.py check              # 环境检查
  python run.py test               # 运行测试
  python run.py db-init            # 仅初始化数据库

参数:
  --host HOST         监听地址 (默认: 0.0.0.0)
  --port PORT         监听端口 (默认: 8020)
  --no-browser        不自动打开浏览器
  --reload            开发模式热重载
  --log-file FILE     日志输出文件 (默认: log/legal-mail.log)
"""
import argparse
import sys
import os
import signal
import time
import subprocess
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

LOG_DIR = PROJECT_ROOT / "log"
PID_FILE = LOG_DIR / "legal-mail.pid"


def _ensure_venv():
    """确认虚拟环境存在且激活"""
    # 检查是否已在 venv 中
    in_venv = hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    )
    if in_venv:
        return sys.executable

    # 尝试查找项目 venv
    for venv_dir in [PROJECT_ROOT / "venv", PROJECT_ROOT / ".venv"]:
        for py_name in ["bin/python3", "bin/python", "Scripts/python.exe"]:
            py_path = venv_dir / py_name
            if py_path.exists():
                return str(py_path)

    return sys.executable  # 退回系统 Python


def cmd_install(args):
    """安装/更新项目依赖"""
    python = _ensure_venv()
    print(f"Python: {python}")
    print("安装依赖...")
    subprocess.check_call([python, "-m", "pip", "install", "-e", ".", "--quiet"],
                          cwd=str(PROJECT_ROOT))
    # 开发依赖
    subprocess.check_call([python, "-m", "pip", "install", "pytest", "--quiet"],
                          cwd=str(PROJECT_ROOT))
    print("✅ 依赖安装完成")


def cmd_check(args):
    """环境检查"""
    python = _ensure_venv()
    print(f"Python:  {python}")
    print(f"项目目录: {PROJECT_ROOT}")

    # Python 版本
    ver = subprocess.check_output([python, "--version"], text=True).strip()
    print(f"版本:    {ver}")

    # 检查关键依赖
    deps = {"fastapi": "fastapi", "uvicorn": "uvicorn", "sqlalchemy": "sqlalchemy",
            "httpx": "httpx", "cryptography": "cryptography",
            "apscheduler": "apscheduler", "jinja2": "jinja2", "python-docx": "docx",
            "pymupdf": "fitz", "openpyxl": "openpyxl"}
    missing = []
    for name, mod in deps.items():
        try:
            __import__(mod)
            print(f"  ✓ {name}")
        except ImportError:
            print(f"  ✗ {name} — 缺失")
            missing.append(name)

    # 检查数据目录
    data_dir = PROJECT_ROOT / "data"
    if data_dir.exists():
        db_file = data_dir / "legal_mail.db"
        if db_file.exists():
            size_mb = db_file.stat().st_size / (1024 * 1024)
            print(f"数据库:   {db_file} ({size_mb:.1f} MB)")
        else:
            print("数据库:   未创建（首次启动时自动创建）")
    else:
        print("数据目录: 未创建")

    # antiword
    antiword_path = shutil.which("antiword")
    if antiword_path:
        print(f"  ✓ antiword ({antiword_path})")
    else:
        print("  - antiword 未安装 (.doc 提取降级)")

    if missing:
        print(f"\n⚠️  缺失 {len(missing)} 个依赖，运行 'python run.py install' 安装")
    else:
        print("\n✅ 环境正常")


def cmd_db_init(args):
    """初始化数据库"""
    python = _ensure_venv()
    print("初始化数据库...")
    subprocess.check_call(
        [python, "-c", "from app.database import init_db; init_db(); print('✅ 数据库初始化完成')"],
        cwd=str(PROJECT_ROOT),
    )


def cmd_test(args):
    """运行测试"""
    python = _ensure_venv()
    print("运行测试...")
    result = subprocess.run(
        [python, "-m", "pytest", "tests/", "-v", "--tb=short"],
        cwd=str(PROJECT_ROOT),
    )
    sys.exit(result.returncode)


def _resolve_port(args, default=8020) -> int:
    """解析端口：CLI > 数据库设置 > 默认"""
    port = args.port
    if port == default:
        # 尝试从数据库读取已保存端口
        try:
            sys.path.insert(0, str(PROJECT_ROOT))
            from app.database import SessionLocal
            from app.models import DefaultConfig
            db = SessionLocal()
            try:
                cfg = db.query(DefaultConfig).filter_by(key="system_port").first()
                if cfg and cfg.value and cfg.value.strip().isdigit():
                    port = int(cfg.value)
            finally:
                db.close()
        except Exception:
            pass
    return port


def cmd_start(args):
    """启动服务"""
    python = _ensure_venv()
    port = _resolve_port(args)

    if args.daemon:
        # 后台守护进程模式
        LOG_DIR.mkdir(exist_ok=True)
        log_file = args.log_file or str(LOG_DIR / "legal-mail.log")

        # 检查是否已在运行
        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
                os.kill(pid, 0)  # 检查进程是否存在
                print(f"⚠️  服务已在运行 (PID: {pid})")
                print(f"   使用 'python run.py stop' 停止")
                print(f"   使用 'python run.py restart' 重启")
                sys.exit(1)
            except (OSError, ValueError):
                PID_FILE.unlink(missing_ok=True)

        print(f"后台启动 (端口: {port}, 日志: {log_file})")
        with open(log_file, "a") as lf:
            lf.write(f"\n--- 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            proc = subprocess.Popen(
                [python, "-m", "uvicorn", "app.main:app",
                 "--host", args.host or "0.0.0.0",
                 "--port", str(port),
                 "--log-level", "info"],
                cwd=str(PROJECT_ROOT),
                stdout=lf, stderr=lf,
                start_new_session=True,
            )

        PID_FILE.write_text(str(proc.pid))
        print(f"\n╔══════════════════════════════════════════╗")
        print(f"║  ⚖️  邮件智能分析转发系统 v2.0.0            ║")
        print(f"╠══════════════════════════════════════════╣")
        print(f"║  PID:      {proc.pid:<6}                    ║")
        print(f"║  Web UI:   http://localhost:{port}            ║")
        print(f"║  日志:     {log_file}")
        print(f"║  停止:     python run.py stop               ║")
        print(f"╚══════════════════════════════════════════╝")
    else:
        # 前台模式
        if not args.no_browser:
            try:
                import webbrowser
                webbrowser.open(f"http://localhost:{port}")
            except Exception:
                pass

        print(f"""
╔══════════════════════════════════════════╗
║  ⚖️  邮件智能分析转发系统 v2.0.0            ║
║  Legal Mail Router                       ║
╠══════════════════════════════════════════╣
║  Web UI:    http://localhost:{port:<5}       ║
║  API 文档:  已禁用（docs/redoc/openapi 关闭）   ║
║  按 Ctrl+C 停止                           ║
╚══════════════════════════════════════════╝
""")
        import uvicorn
        uvicorn.run(
            "app.main:app",
            host=args.host or "0.0.0.0",
            port=port,
            reload=args.reload,
            log_level="info",
        )


def cmd_stop(args):
    """停止后台服务"""
    if not PID_FILE.exists():
        print("未找到运行中的服务 (PID 文件不存在)")
        return

    try:
        pid = int(PID_FILE.read_text().strip())

        # 跨平台发送终止信号
        if sys.platform == "win32":
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.kill(pid, signal.SIGTERM)
        print(f"已发送停止信号 (PID: {pid})")

        # 等待进程退出
        for _ in range(10):
            time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except OSError:
                print("✅ 服务已停止")
                PID_FILE.unlink(missing_ok=True)
                return
        print("⚠️  进程未响应，强制终止...")
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True)
        else:
            os.kill(pid, signal.SIGKILL)
        PID_FILE.unlink(missing_ok=True)
    except (ValueError, OSError) as e:
        print(f"停止失败: {e}")
        PID_FILE.unlink(missing_ok=True)


def cmd_restart(args):
    """重启后台服务"""
    cmd_stop(args)
    time.sleep(1)
    cmd_start(args)


def cmd_status(args):
    """查看服务状态"""
    if not PID_FILE.exists():
        print("状态: 未运行")
        return

    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        port = _resolve_port(args)
        print(f"状态: 运行中 (PID: {pid})")
        print(f"Web UI: http://localhost:{port}")
        print(f"健康检查: http://localhost:{port}/health")
    except (ValueError, OSError):
        print("状态: 未运行 (PID 文件残留)")
        PID_FILE.unlink(missing_ok=True)


def main():
    # Windows 控制台默认 GBK 无法输出 ⚖️ 等 Emoji，强制 UTF-8 避免启动脚本崩溃
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="邮件智能分析转发系统 — 启动与管理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python run.py                    # 前台启动
  python run.py start --daemon     # 后台启动
  python run.py stop               # 停止
  python run.py restart            # 重启
  python run.py status             # 查看状态
  python run.py install            # 安装依赖
  python run.py check              # 环境检查
  python run.py test               # 运行测试
""",
    )
    subparsers = parser.add_subparsers(dest="command")

    # start
    p_start = subparsers.add_parser("start", help="启动服务")
    p_start.add_argument("--host", default="0.0.0.0")
    p_start.add_argument("--port", type=int, default=8020)
    p_start.add_argument("--no-browser", action="store_true")
    p_start.add_argument("--reload", action="store_true")
    p_start.add_argument("--daemon", action="store_true", help="后台守护进程模式")
    p_start.add_argument("--log-file", help="日志文件路径")

    # stop
    subparsers.add_parser("stop", help="停止后台服务")

    # restart
    p_restart = subparsers.add_parser("restart", help="重启后台服务")
    p_restart.add_argument("--host", default="0.0.0.0")
    p_restart.add_argument("--port", type=int, default=8020)
    p_restart.add_argument("--daemon", action="store_true", default=True)
    p_restart.add_argument("--log-file", help="日志文件路径")

    # status
    subparsers.add_parser("status", help="查看服务状态")

    # install / check / db-init / test
    subparsers.add_parser("install", help="安装项目依赖")
    subparsers.add_parser("check", help="环境检查")
    subparsers.add_parser("db-init", help="初始化数据库")
    subparsers.add_parser("test", help="运行测试")

    args = parser.parse_args()

    commands = {
        "start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
        "status": cmd_status, "install": cmd_install, "check": cmd_check,
        "db-init": cmd_db_init, "test": cmd_test,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        # 默认：前台启动（兼容旧用法）
        if not hasattr(args, 'host'):
            args.host = "0.0.0.0"
        if not hasattr(args, 'port'):
            args.port = 8020
        if not hasattr(args, 'no_browser'):
            args.no_browser = False
        if not hasattr(args, 'reload'):
            args.reload = False
        if not hasattr(args, 'daemon'):
            args.daemon = False
        cmd_start(args)


if __name__ == "__main__":
    main()
