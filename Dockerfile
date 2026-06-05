FROM python:3.12-slim

LABEL maintainer="legal-mail-router"
LABEL description="文书分拣系统 - AI 驱动的法律邮件自动分拣与转发"

WORKDIR /app

# 安装系统依赖（PyMuPDF 需要）+ antiword 用于 .doc 文本提取
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tzdata \
        antiword \
    && rm -rf /var/lib/apt/lists/*

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 安装 Python 依赖
COPY pyproject.toml .
RUN pip install --no-cache-dir -e . && \
    pip install --no-cache-dir uvicorn

# 复制应用代码
COPY app/ ./app/
COPY templates/ ./templates/
COPY static/ ./static/
COPY LLM提示词.md ./

# 创建数据目录
RUN mkdir -p /app/data/attachments

# 暴露端口
EXPOSE 8888

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8888/health')" || exit 1

# 启动
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8888"]
