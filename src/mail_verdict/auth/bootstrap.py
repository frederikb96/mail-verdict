"""
The instance owner, made consistent with configuration at startup.

The owner row (OWNER_USER_ID) is created by the 0050 migration; this restores
it if it is missing, and links every subject in auth.owner_subjects to it, so
the users tables agree with what the resolver already decides from config
alone. A subject linked to another user is moved to the owner -- the
configuration is the authority -- and the move is logged.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mail_verdict.auth.principal import OWNER_USER_ID
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import User, UserIdentity

logger = logging.getLogger(__name__)


async def ensure_owner(db: DatabaseConnection, owner_subjects: list[str]) -> None:
    """
    Create the owner row if missing and link the owner subjects to it.

    Args:
        db: The initialized database connection.
        owner_subjects: auth.owner_subjects.
    """
    async with db.session() as session:
        await session.execute(
            pg_insert(User)
            .values(id=OWNER_USER_ID, display_name="Owner", role="admin")
            .on_conflict_do_nothing(index_elements=[User.id])
        )
        linked = dict(
            (
                await session.execute(
                    select(UserIdentity.subject, UserIdentity.user_id).where(
                        UserIdentity.subject.in_(owner_subjects)
                    )
                )
            ).tuples().all()
        )
        for subject in owner_subjects:
            previous = linked.get(subject)
            if previous == OWNER_USER_ID:
                continue
            if previous is not None:
                logger.warning(
                    "Owner subject was linked to user %s; linking it to the owner instead",
                    previous,
                )
            await session.execute(
                pg_insert(UserIdentity)
                .values(subject=subject, user_id=OWNER_USER_ID)
                .on_conflict_do_update(
                    index_elements=[UserIdentity.subject], set_={"user_id": OWNER_USER_ID},
                )
            )
