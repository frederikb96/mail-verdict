#!/bin/bash
set -e
cd /app
exec python -m mail_verdict "$@"
