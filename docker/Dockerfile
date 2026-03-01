# MailVerdict Dockerfile
# Multi-stage build: base -> dev (hot-reload) -> prod (baked source)

# =============================================================================
# Base stage: Python runtime + uv
# =============================================================================
FROM python:3.13-slim AS base

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -u 1000 appuser

WORKDIR /app

# uv for fast dependency installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# =============================================================================
# Dev stage: deps cached in image, source mounted at runtime
# =============================================================================
FROM base AS dev

# Install all deps including dev extras (cached when pyproject.toml unchanged)
COPY pyproject.toml README.md ./
RUN mkdir -p src/mail_verdict && touch src/mail_verdict/__init__.py
RUN uv pip install --system --no-cache-dir -e ".[dev]"

HEALTHCHECK --interval=10s --timeout=5s --retries=3 --start-period=15s \
    CMD curl -sf http://localhost:8080/api/health || exit 1

COPY docker/entrypoint-dev.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# =============================================================================
# Prod stage: source baked in, minimal image
# =============================================================================
FROM base AS prod

# Layer 1: Install deps only (cached when pyproject.toml unchanged)
COPY pyproject.toml README.md ./
RUN mkdir -p src/mail_verdict && touch src/mail_verdict/__init__.py \
    && uv pip install --system --no-cache-dir .

# Layer 2: Copy actual source and config
COPY src/ ./src/
COPY config/ ./config/

# Layer 3: Reinstall without deps (fast — just re-links entry points)
RUN uv pip install --system --no-cache-dir --no-deps .

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=30s \
    CMD curl -sf http://localhost:8080/api/health || exit 1

COPY docker/entrypoint-prod.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
USER appuser
ENTRYPOINT ["/entrypoint.sh"]
