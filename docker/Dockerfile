# MailVerdict Dockerfile
# Multi-stage build: base -> dev (hot-reload) -> prod (baked source)

# =============================================================================
# Base stage: Python runtime + uv
# =============================================================================
FROM python:3.13-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gosu \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 appuser

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# =============================================================================
# Dev stage: deps cached in image, source mounted at runtime
# =============================================================================
FROM base AS dev

COPY pyproject.toml README.md ./
RUN mkdir -p src/mail_verdict && touch src/mail_verdict/__init__.py
RUN uv pip install --system --no-cache-dir -e ".[dev]"

# Make site-packages writable for uid 1000 (userns_mode: keep-id compatibility)
RUN SITE_PKG=$(python3 -c "import sysconfig; print(sysconfig.get_path('purelib'))") && \
    chmod -R a+rw "$SITE_PKG/_mail_verdict.pth" "$SITE_PKG/mail_verdict.egg-info" 2>/dev/null || true && \
    chown -R 1000:1000 /app

HEALTHCHECK --interval=10s --timeout=5s --retries=20 --start-period=30s \
    CMD curl -sf http://localhost:8080/api/health || exit 1

COPY docker/entrypoint-dev.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# =============================================================================
# UI build stage: Next.js static export
# =============================================================================
FROM node:24-slim AS ui-build

WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --ignore-scripts
COPY ui/ ./
RUN npm run build

# =============================================================================
# Prod stage: source baked in, minimal image
# =============================================================================
FROM base AS prod

COPY pyproject.toml README.md ./
RUN mkdir -p src/mail_verdict && touch src/mail_verdict/__init__.py \
    && uv pip install --system --no-cache-dir .

COPY src/ ./src/
COPY config/ ./config/
COPY alembic/ ./alembic/
COPY alembic.ini ./
COPY --from=ui-build /ui/build ./ui/build

RUN uv pip install --system --no-cache-dir --no-deps .

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=30s \
    CMD curl -sf http://localhost:8080/api/health || exit 1

COPY docker/entrypoint-prod.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
USER appuser
ENTRYPOINT ["/entrypoint.sh"]
