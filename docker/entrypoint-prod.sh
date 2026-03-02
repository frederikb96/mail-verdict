#!/bin/bash
set -e
cd /app
alembic upgrade head
exec python -m mail_verdict "$@"
