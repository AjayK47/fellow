# syntax=docker/dockerfile:1.7
FROM node:22-alpine AS console
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.6.17 AS uv

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PATH="/app/.venv/bin:$PATH" \
    TODA_DATA_DIR=/var/lib/toda \
    TODA_WORKSPACE_ROOT=/var/lib/toda/workspaces \
    TODA_HOST=0.0.0.0 \
    TODA_PORT=8080

RUN apt-get update && apt-get install --no-install-recommends -y \
      graphviz \
      libgdk-pixbuf-2.0-0 \
      libpango-1.0-0 \
      libpangocairo-1.0-0 \
      poppler-utils \
      shared-mime-info \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 toda \
    && mkdir -p /var/lib/toda \
    && chown toda:toda /var/lib/toda

COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
COPY --from=console /web/dist/ ./src/toda/static/console/
RUN uv sync --frozen --no-dev --no-editable

USER toda
VOLUME ["/var/lib/toda"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]
CMD ["toda", "gateway"]
