"""
Account state machine: manages lifecycle transitions.

States: created -> syncing -> seeding -> active -> error
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from mail_verdict.database.models import Account, AccountState

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

_VALID_TRANSITIONS: dict[AccountState, set[AccountState]] = {
    AccountState.CREATED: {AccountState.SYNCING, AccountState.ERROR},
    AccountState.SYNCING: {AccountState.SEEDING, AccountState.ERROR},
    AccountState.SEEDING: {AccountState.ACTIVE, AccountState.ERROR},
    AccountState.ACTIVE: {AccountState.ERROR, AccountState.SYNCING},
    AccountState.ERROR: {AccountState.SYNCING, AccountState.CREATED},
}

MAX_CONSECUTIVE_ERRORS = 5


async def transition_account(
    db: DatabaseConnection,
    account_id: uuid.UUID,
    new_state: AccountState,
) -> bool:
    """
    Transition an account to a new state if the transition is valid.

    Args:
        db: Database connection
        account_id: Account to transition
        new_state: Target state

    Returns:
        True if transition succeeded, False if invalid
    """
    async with db.session() as session:
        result = await session.execute(
            select(Account.state).where(Account.id == account_id)
        )
        row = result.one_or_none()
        if row is None:
            return False

        current = row[0]
        valid_targets = _VALID_TRANSITIONS.get(current, set())
        if new_state not in valid_targets:
            logger.warning(
                "Invalid state transition",
                extra={
                    "account_id": str(account_id)[:8],
                    "current": current.value if isinstance(current, AccountState) else str(current),
                    "target": new_state.value,
                },
            )
            return False

        await session.execute(
            update(Account).where(Account.id == account_id).values(state=new_state)
        )
        logger.info(
            "Account state transition",
            extra={
                "account_id": str(account_id)[:8],
                "from": current.value if isinstance(current, AccountState) else str(current),
                "to": new_state.value,
            },
        )
        return True


async def record_account_error(
    db: DatabaseConnection,
    account_id: uuid.UUID,
    error_msg: str,
) -> None:
    """
    Record an error for an account, transitioning to ERROR state if threshold exceeded.

    Uses JobState error_count for the account's imap_sync job as the counter.

    Args:
        db: Database connection
        account_id: Account that encountered an error
        error_msg: Error description
    """
    from mail_verdict.database.models import JobState

    async with db.session() as session:
        result = await session.execute(
            select(JobState).where(
                JobState.name == "imap_sync",
                JobState.account_id == account_id,
            )
        )
        job_state = result.scalar_one_or_none()
        error_count = (job_state.error_count + 1) if job_state else 1

    if error_count >= MAX_CONSECUTIVE_ERRORS:
        await transition_account(db, account_id, AccountState.ERROR)
        logger.warning(
            "Account moved to error state after %d consecutive failures",
            error_count,
            extra={"account_id": str(account_id)[:8], "last_error": error_msg},
        )
