"""
The notification badge, computed once, here.

What it counts: every unacknowledged write failure (sync_notifications,
across every account, inactive ones included), every unseen alert that is
not new mail, and unseen new-mail alerts only while
settings.mail.bell_badge_counts_new_mail is on. Unseen alerts are scoped
to the folders the device alerts for -- a push subscription's own
alert_folder_ids, arrival folders when it has none, or a browser's
explicit folder list.

The web bell reads it through GET /api/alerts/badge and a native push
carries it, so a phone asleep and a browser open agree on the number.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from mail_verdict.database.repository import AlertRepository, SyncNotificationRepository
from mail_verdict.postimap.contract import read_postimap_info, supports_sync_notifications

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.database.models import PushSubscription
    from mail_verdict.settings.service import SettingsService


async def badge_count(
    db: DatabaseConnection,
    settings_service: SettingsService,
    *,
    subscription: PushSubscription | None = None,
    folder_ids: list[uuid.UUID] | None = None,
) -> int:
    """
    The badge for one device.

    Args:
        db: Database connection
        settings_service: Where bell_badge_counts_new_mail is read
        subscription: A registered device -- its own folder scope applies
        folder_ids: A browser's own folder scope, when there is no
            subscription; None leaves alerts unscoped. Ignored when
            subscription is given.

    Returns:
        The number the device's badge shows
    """
    if subscription is not None:
        folder_ids = subscription.alert_folder_ids
        arrival_folders_only = folder_ids is None
    else:
        arrival_folders_only = False
    by_kind = await AlertRepository(db).unseen_counts_by_kind(
        folder_ids=folder_ids, arrival_folders_only=arrival_folders_only,
    )
    counts_new_mail = bool(settings_service.get("mail")["bell_badge_counts_new_mail"])
    count = sum(n for kind, n in by_kind.items() if counts_new_mail or kind != "mail")

    async with db.session() as session:
        info = await read_postimap_info(session)
    if info is not None and supports_sync_notifications(info):
        count += await SyncNotificationRepository(db).unacknowledged_count_all()
    return count
