#!/bin/bash
set -e
cd /app

# Reinstall package in editable mode (picks up mounted source changes)
uv pip install --system --no-deps -e .

exec uvicorn mail_verdict.server:create_app \
    --host 0.0.0.0 \
    --port 8080 \
    --reload \
    --reload-dir /app/src \
    --factory \
    "$@"
