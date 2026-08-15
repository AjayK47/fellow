# syntax=docker/dockerfile:1.7
FROM node:22-alpine AS console
WORKDIR /web
# pnpm, matching the tracked lockfile. This stage used to run `npm ci`
# against web/package-lock.json, which had been deleted when the local
# workflow moved to pnpm -- so the image could not be built from a clean
# clone at all, and only worked locally because node_modules already existed.
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm run build

FROM ghcr.io/astral-sh/uv:0.6.17 AS uv

FROM python:3.12-slim AS runtime
# TODA_CONFIG is the only environment variable Toda reads -- a config file
# cannot tell you where it lives. Everything else (data dir, host, port)
# comes from that file. The TODA_DATA_DIR / TODA_HOST / TODA_PORT variables
# that used to be set here stopped being read when config.json became the
# single source of configuration, which meant the container silently fell
# back to defaults and bound 127.0.0.1 -- unreachable from outside.
#
# Mount a config.json at /etc/toda/config.json with at least:
#   {"server": {"host": "0.0.0.0", "port": 8080},
#    "storage": {"data_dir": "/var/lib/toda"},
#    "organization": {"id": "...", "owner_email": "..."}}
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PATH="/app/.venv/bin:$PATH" \
    TODA_CONFIG=/etc/toda/config.json

RUN apt-get update && apt-get install --no-install-recommends -y \
      graphviz \
      libgdk-pixbuf-2.0-0 \
      libpango-1.0-0 \
      libpangocairo-1.0-0 \
      poppler-utils \
      shared-mime-info \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 toda \
    && mkdir -p /var/lib/toda /etc/toda \
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
