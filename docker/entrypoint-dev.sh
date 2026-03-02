#!/bin/bash
set -e
cd /app

# Reinstall package in editable mode (picks up mounted source changes).
# Skip if it fails (e.g. userns_mode: keep-id permission issues) — the
# build-time install is still present and source is volume-mounted.
uv pip install --system --no-deps -e . 2>/dev/null || true

# Run database migrations if alembic is configured
if [ -f alembic.ini ]; then
    alembic upgrade head || true
fi

# With userns_mode: keep-id, we already run as the host user (uid 1000).
# gosu is only needed when running as root to drop privileges.
if [ "$(id -u)" = "0" ]; then
    exec gosu appuser uvicorn mail_verdict.server:create_app \
        --host 0.0.0.0 \
        --port 8080 \
        --reload \
        --reload-dir /app/src \
        --factory \
        "$@"
else
    exec uvicorn mail_verdict.server:create_app \
        --host 0.0.0.0 \
        --port 8080 \
        --reload \
        --reload-dir /app/src \
        --factory \
        "$@"
fi
