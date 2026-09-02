"""
API route aggregation.

Collects all FastAPI routers into a single list for mounting.
"""

from __future__ import annotations

from fastapi import APIRouter

from mail_verdict.api.accounts import router as accounts_router
from mail_verdict.api.calendar_events import router as calendar_events_router
from mail_verdict.api.calendars import addressbooks_router
from mail_verdict.api.calendars import links_router as calendar_links_router
from mail_verdict.api.calendars import router as calendars_router
from mail_verdict.api.contacts import router as contacts_router
from mail_verdict.api.dav_accounts import router as dav_accounts_router
from mail_verdict.api.embeddings import router as embeddings_router
from mail_verdict.api.folder_management import folder_prefs_router
from mail_verdict.api.folder_management import router as folder_management_router
from mail_verdict.api.identities import router as identities_router
from mail_verdict.api.image_exceptions import router as image_exceptions_router
from mail_verdict.api.mails import account_router as mails_account_router
from mail_verdict.api.mails import router as mails_router
from mail_verdict.api.notifications import router as notifications_router
from mail_verdict.api.outbox import router as outbox_router
from mail_verdict.api.pipeline import router as pipeline_router
from mail_verdict.api.queues import router as queues_router
from mail_verdict.api.runs import router as runs_router
from mail_verdict.api.search import router as search_router
from mail_verdict.api.settings_api import router as settings_router
from mail_verdict.api.stats import router as stats_router
from mail_verdict.api.unified import account_router as unified_account_router
from mail_verdict.api.unified import unified_router
from mail_verdict.api.verdicts import router as verdicts_router

# Aggregate all API routers
all_routers: list[APIRouter] = [
    mails_router,
    mails_account_router,
    notifications_router,
    outbox_router,
    search_router,
    accounts_router,
    identities_router,
    image_exceptions_router,
    folder_management_router,
    folder_prefs_router,
    unified_account_router,
    unified_router,
    settings_router,
    verdicts_router,
    stats_router,
    queues_router,
    embeddings_router,
    runs_router,
    pipeline_router,
    dav_accounts_router,
    calendars_router,
    calendar_links_router,
    addressbooks_router,
    contacts_router,
    calendar_events_router,
]
