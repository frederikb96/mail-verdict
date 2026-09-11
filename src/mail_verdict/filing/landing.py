"""
A message landing in a folder, read off a postimap event -- the one
place that knows which events mean that, for anything that has to react
to mail arriving somewhere rather than only to new mail.

Two event shapes land a message: an insert, which is new mail or the
destination half of a move made in another mail client (PostIMAP mirrors
that as an expunge in the source plus an insert in the destination --
consumer contract, "Moving a message"), and an update whose `changed`
includes folder_id, a move made through the contract by this application
or its pipeline. Both carry the destination as folder_id.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mail_verdict.database.repository import FolderRepository

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.postimap.listener import PostimapEvent


@dataclass(frozen=True)
class Landing:
    """A message that has just arrived in a folder."""

    account_id: uuid.UUID
    message_id: uuid.UUID
    folder_id: uuid.UUID


def landing_from_event(event: PostimapEvent) -> Landing | None:
    """
    The landing a message event describes, if it describes one.

    Args:
        event: Any parsed postimap_events payload

    Returns:
        The landing, or None for an event that puts no message anywhere
        (a flag change, a delete, a non-message event, or an unparseable id)
    """
    if event.type != "message" or not event.folder_id:
        return None
    if event.op != "insert" and not (event.op == "update" and "folder_id" in event.changed):
        return None
    try:
        return Landing(
            account_id=uuid.UUID(event.account_id),
            message_id=uuid.UUID(event.id),
            folder_id=uuid.UUID(event.folder_id),
        )
    except ValueError:
        return None


async def landing_role(
    db: DatabaseConnection, landing: Landing, roles: tuple[str, ...],
) -> str | None:
    """
    Which of `roles` the folder a message landed in plays for its account.

    Resolved with FolderRepository.resolve_special_folder, so a role means
    the same folder here that this application itself files into -- the
    effective special_use, or the well-known-name fallback when no folder
    carries the flag.

    Args:
        db: Database connection
        landing: Where the message landed
        roles: Candidate roles, e.g. ("archive", "trash")

    Returns:
        The first role whose folder is the landing folder, or None
    """
    repo = FolderRepository(db)
    for role in roles:
        if await repo.resolve_special_folder(landing.account_id, role) == landing.folder_id:
            return role
    return None
