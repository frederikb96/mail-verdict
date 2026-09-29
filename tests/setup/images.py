"""
Single source of truth for every container image tag used by the test
suite. Kept separate from application code so Renovate can track these
independently of the compose files' own image pins.
"""

from __future__ import annotations

import os

# renovate: datasource=docker depName=pgvector/pgvector versioning=docker
POSTGRES_IMAGE = "pgvector/pgvector:pg18"

# renovate: datasource=docker depName=ghcr.io/frederikb96/postimap versioning=docker
#
# Overridable via MAIL_VERDICT_TEST_POSTIMAP_IMAGE for a local run against a
# PostIMAP build the pinned tag does not carry yet -- a capability still on
# an upstream branch, say. Never set in CI or in any committed config: the
# pinned default is what every ordinary run, and Renovate's own tracking,
# uses.
POSTIMAP_IMAGE = os.environ.get(
    "MAIL_VERDICT_TEST_POSTIMAP_IMAGE", "ghcr.io/frederikb96/postimap:1.10.0",
)

# renovate: datasource=docker depName=dovecot/dovecot versioning=docker
DOVECOT_IMAGE = "dovecot/dovecot:2.4.5"

# renovate: datasource=docker depName=axllent/mailpit versioning=docker
MAILPIT_IMAGE = "axllent/mailpit:v1.28.3"

# renovate: datasource=docker depName=tomsquest/docker-radicale versioning=docker
RADICALE_IMAGE = "tomsquest/docker-radicale:3.7.6.0"
