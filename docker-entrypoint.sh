#!/bin/sh
# docker-entrypoint.sh — 读取 PORT 环境变量并启动 uvicorn
set -e

PORT="${PORT:-8020}"

exec python -m uvicorn app.main:app \
    --host 0.0.0.0 \
    --port "${PORT}"
