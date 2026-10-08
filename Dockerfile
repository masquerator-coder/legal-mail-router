FROM python:3.12-slim-bookworm

LABEL maintainer="legal-mail-router"
LABEL description="邮件智能分析转发系统 - AI 驱动的法律邮件自动分拣与转发"

WORKDIR /app

# 镜像源可通过 --build-arg 覆盖（默认国内清华源，外网部署可换官方源）
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# 安装系统依赖（PyMuPDF 需要）+ antiword 用于 .doc 文本提取
# + p7zip-full / unar 用于解压 rar（含 RAR5）/ 7z 附件
# （unrar-free 仅支持 RAR4，modern RAR5 需 unar，故用 unar 替代）
# + libreoffice-writer-nogui 用于把 .doc 原文转成 .docx，使修改版文书
#   能保留原文格式（无 GUI 变体，配合 --no-install-recommends 跳过
#   Java/字体等推荐包；缺失时应用自动降级为纯文本重建，不影响启动）
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list 2>/dev/null; \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        tzdata \
        antiword \
        libreoffice-writer-nogui \
        p7zip-full \
        unar \
        git \
    && rm -rf /var/lib/apt/lists/*

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 安装 Python 依赖（镜像源可用 --build-arg PIP_INDEX_URL 覆盖）
COPY pyproject.toml .
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -e . && \
    pip install --no-cache-dir -i ${PIP_INDEX_URL} uvicorn

# 复制应用代码（config/ 已合并到 app/services/ 中）
COPY app/ ./app/
COPY templates/ ./templates/
COPY static/ ./static/
COPY LLM提示词模板.md ./
COPY 文书类型.conf ./
COPY 分析提示词/ ./分析提示词/

# 创建数据目录并以非 root 用户运行（降低容器被攻破后的影响面）
RUN mkdir -p /app/data/attachments && \
    useradd -r -m -u 10001 appuser && \
    chown -R appuser:appuser /app/data /app

# 默认端口（可通过 docker run -e PORT=9000 或 docker-compose 覆盖）
ENV PORT=8020

# 暴露端口（EXPOSE 仅作文档用途，实际端口由 PORT 环境变量控制）
EXPOSE 8020

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\",\"8020\")}/health')" || exit 1

# 启动（使用 entrypoint 脚本支持 PORT 环境变量）
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh
USER appuser
ENTRYPOINT ["/docker-entrypoint.sh"]
