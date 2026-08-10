#!/usr/bin/env bash
# ──────────────────────────────────────────────────
# 邮件智能分析转发系统 — 服务管理脚本
#
# 用法:
#   ./start.sh             前台启动
#   ./start.sh start       后台启动
#   ./start.sh stop        停止
#   ./start.sh restart     重启
#   ./start.sh status      状态
#   ./start.sh logs        查看日志
#   ./start.sh install     安装依赖
#   ./start.sh test        运行测试
# ──────────────────────────────────────────────────
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

VENV_PYTHON=""
for py in venv/bin/python3 venv/bin/python .venv/bin/python3 .venv/bin/python; do
    if [ -x "$PROJECT_DIR/$py" ]; then
        VENV_PYTHON="$PROJECT_DIR/$py"
        break
    fi
done
PYTHON="${VENV_PYTHON:-python3}"

PID_FILE="$PROJECT_DIR/log/legal-mail.pid"
LOG_FILE="$PROJECT_DIR/log/legal-mail.log"
mkdir -p "$PROJECT_DIR/log"

# ════════════════════════════ helpers ════════════════════════════

_usage() {
    echo "用法: $0 {start|stop|restart|status|logs|install|test}"
    echo ""
    echo "  start    后台启动服务"
    echo "  stop     停止服务"
    echo "  restart  重启服务"
    echo "  status   查看运行状态"
    echo "  logs     查看实时日志"
    echo "  install  安装依赖"
    echo "  test     运行测试"
    echo ""
    echo "  不带参数: 前台启动（Ctrl+C 停止）"
    exit 0
}

_running() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

_port_from_db() {
    local port=8020
    if [ -f "$PROJECT_DIR/data/legal_mail.db" ]; then
        local db_port
        db_port=$(sqlite3 "$PROJECT_DIR/data/legal_mail.db" \
            "SELECT value FROM default_config WHERE key='system_port';" 2>/dev/null || echo "")
        if [ -n "$db_port" ] && [ "$db_port" -gt 0 ] 2>/dev/null; then
            port="$db_port"
        fi
    fi
    echo "$port"
}

# ════════════════════════════ commands ════════════════════════════

cmd_install() {
    echo "=== 安装依赖 ==="
    "$PYTHON" -m pip install -e . --quiet
    "$PYTHON" -m pip install pytest --quiet
    echo "✅ 完成"
}

cmd_test() {
    echo "=== 运行测试 ==="
    "$PYTHON" -m pytest tests/ -v --tb=short
}

cmd_start() {
    if _running; then
        echo "⚠️  服务已在运行 (PID: $(cat "$PID_FILE"))"
        return 1
    fi

    local port
    port=$(_port_from_db)
    echo "=== 后台启动 (端口: $port) ==="
    echo "启动时间: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
    nohup "$PYTHON" -m uvicorn app.main:app \
        --host 0.0.0.0 \
        --port "$port" \
        --log-level info \
        >> "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"

    sleep 2
    if _running; then
        echo ""
        echo "╔══════════════════════════════════════════╗"
        echo "║  ⚖️  邮件智能分析转发系统 v2.0.0            ║"
        echo "╠══════════════════════════════════════════╣"
        echo "║  PID:      $pid                       ║"
        echo "║  Web UI:   http://localhost:$port            ║"
        printf '║  API:      http://localhost:%s/docs   ║\n' "$port"
        echo "║  日志:     $LOG_FILE"
        echo "║  停止:     ./start.sh stop               ║"
        echo "╚══════════════════════════════════════════╝"
    else
        echo "❌ 启动失败，查看日志: tail -50 $LOG_FILE"
        rm -f "$PID_FILE"
        return 1
    fi
}

cmd_stop() {
    if ! _running; then
        echo "服务未运行"
        rm -f "$PID_FILE"
        return 0
    fi
    local pid
    pid=$(cat "$PID_FILE")
    echo "正在停止 (PID: $pid)..."
    kill "$pid" 2>/dev/null || true

    for _ in $(seq 1 10); do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "✅ 已停止"
            rm -f "$PID_FILE"
            return 0
        fi
        sleep 0.5
    done

    echo "⚠️  强制终止..."
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$PID_FILE"
    echo "✅ 已强制停止"
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

cmd_status() {
    if _running; then
        local pid port
        pid=$(cat "$PID_FILE")
        port=$(_port_from_db)
        echo "✅ 运行中"
        echo "   PID:     $pid"
        echo "   端口:    $port"
        echo "   Web UI:  http://localhost:$port"
        echo "   健康:    http://localhost:$port/health"
        echo "   日志:    $LOG_FILE"
    else
        echo "⏹  未运行"
        rm -f "$PID_FILE"
    fi
}

cmd_logs() {
    if [ -f "$LOG_FILE" ]; then
        tail -f "$LOG_FILE"
    else
        echo "日志文件不存在: $LOG_FILE"
    fi
}

cmd_foreground() {
    local port
    port=$(_port_from_db)
    echo ""
    echo "╔══════════════════════════════════════════╗"
    echo "║  ⚖️  邮件智能分析转发系统 v2.0.0            ║"
    echo "╠══════════════════════════════════════════╣"
    printf '║  Web UI:   http://localhost:%-5s       ║\n' "$port"
    printf '║  API 文档: http://localhost:%s/docs   ║\n' "$port"
    echo "║  按 Ctrl+C 停止                           ║"
    echo "╚══════════════════════════════════════════╝"
    echo ""
    "$PYTHON" -m uvicorn app.main:app --host 0.0.0.0 --port "$port"
}

# ════════════════════════════ main ════════════════════════════

case "${1:-}" in
    start)      cmd_start ;;
    stop)       cmd_stop ;;
    restart)    cmd_restart ;;
    status)     cmd_status ;;
    logs)       cmd_logs ;;
    install)    cmd_install ;;
    test)       cmd_test ;;
    -h|--help)  _usage ;;
    "")         cmd_foreground ;;
    *)          echo "未知命令: $1"; _usage ;;
esac
