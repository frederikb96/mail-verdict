"""
API route aggregation.

Collects all FastAPI routers into a single list for mounting.
"""

from __future__ import annotations

from fastapi import APIRouter

from mail_verdict.api.accounts import router as accounts_router
from mail_verdict.api.jobs import router as jobs_router
from mail_verdict.api.mails import router as mails_router
from mail_verdict.api.rules import router as rules_router
from mail_verdict.api.search import router as search_router
from mail_verdict.api.settings_api import router as settings_router
from mail_verdict.api.stats import router as stats_router
from mail_verdict.api.verdicts import router as verdicts_router

# Aggregate all API routers
all_routers: list[APIRouter] = [
    mails_router,
    search_router,
    accounts_router,
    settings_router,
    rules_router,
    verdicts_router,
    stats_router,
    jobs_router,
]
