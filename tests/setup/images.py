"""
Single source of truth for every container image tag used by the test
suite. Kept separate from application code so Renovate can track these
independently of the compose files' own image pins.
"""

from __future__ import annotations

# renovate: datasource=docker depName=postgres versioning=docker
POSTGRES_IMAGE = "postgres:18-alpine"

# renovate: datasource=docker depName=ghcr.io/frederikb96/postimap versioning=docker
POSTIMAP_IMAGE = "ghcr.io/frederikb96/postimap:1.0.0"

# renovate: datasource=docker depName=dovecot/dovecot versioning=docker
DOVECOT_IMAGE = "dovecot/dovecot:2.4.5"

# renovate: datasource=docker depName=axllent/mailpit versioning=docker
MAILPIT_IMAGE = "axllent/mailpit:latest"
