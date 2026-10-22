FROM node:22-alpine AS frontend

# 前端单独构建，运行镜像只保留 Python 与静态资源。

WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build -- --outDir /frontend/dist

FROM ghcr.io/astral-sh/uv:0.13.1 AS uv

FROM python:3.13-slim AS runtime

COPY --from=uv /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    BARK_DATA_DIR=/data \
    TELEGRAM_SESSION=/data/telegram_session \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
# 先安装锁定依赖，使仅修改源码时能够复用缓存。
RUN uv sync --frozen --no-dev --no-install-project

COPY bark_monitor/ ./bark_monitor/
COPY --from=frontend /frontend/dist/ ./bark_monitor/static/

RUN uv sync --frozen --no-dev \
    && groupadd --system --gid 10001 bark \
    && useradd --system --uid 10001 --gid bark --home-dir /data bark \
    && mkdir -p /data \
    && chown bark:bark /data

USER bark:bark
VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz', timeout=3)" || exit 1

CMD ["bark-dashboard", "--host", "0.0.0.0", "--port", "8787"]
