"""
PostIMAP consumer contract version handshake.

The contract version is a fact about the code -- what this release of
MailVerdict was written against -- not a config value, so it is a constant
here rather than something read from config.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import PostimapInfo

SUPPORTED_CONTRACT_VERSION = 1

# Account deletion (DELETE FROM accounts, cascading to everything hanging off
# it) is granted from this PostIMAP service version onward -- contract_version
# itself stays 1, since granting a new permission breaks nothing a consumer
# already does. Gate the capability on service_version, not contract_version.
MIN_ACCOUNT_DELETE_SERVICE_VERSION = (1, 0, 1)

# Folder creation (INSERT INTO folders) and deletion (UPDATE folders SET
# deleted_at) are granted from this PostIMAP service version onward.
MIN_FOLDER_CRUD_SERVICE_VERSION = (1, 3, 0)

# outbox.replaces_message_id -- editing or sending a draft without leaving a
# duplicate behind -- is granted, and the column exists at all, from this
# PostIMAP service version onward.
MIN_DRAFT_EDIT_SERVICE_VERSION = (1, 4, 0)

# sync_notifications -- the durable, acknowledgeable record of a write that
# never reached the server -- exists, and is granted, from this PostIMAP
# service version onward. Shipped in the same release as folder CRUD.
MIN_SYNC_NOTIFICATIONS_SERVICE_VERSION = (1, 3, 0)


class ContractMismatchError(Exception):
    """Raised when the running PostIMAP's contract_version does not match."""


@dataclass(frozen=True)
class PostimapVersionInfo:
    """The versions reported by PostIMAP's postimap_info table."""

    contract_version: int
    service_version: str


async def read_postimap_info(session: AsyncSession) -> PostimapVersionInfo | None:
    """
    Read the single-row postimap_info table.

    Args:
        session: An open AsyncSession

    Returns:
        PostimapVersionInfo, or None if the row does not exist yet (PostIMAP
        has not finished its own migrations)
    """
    result = await session.execute(select(PostimapInfo).limit(1))
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return PostimapVersionInfo(
        contract_version=row.contract_version,
        service_version=row.service_version,
    )


def assert_contract_version(info: PostimapVersionInfo) -> None:
    """
    Assert that PostIMAP's contract version matches what this build expects.

    A mismatch is fatal, not a degrade-and-continue: the write shapes this
    build relies on (nullable imap_uid, expunged_at, thread_id, outbox, ...)
    are guaranteed only for SUPPORTED_CONTRACT_VERSION.

    Args:
        info: Version info read from postimap_info

    Raises:
        ContractMismatchError: If contract_version does not match
    """
    if info.contract_version != SUPPORTED_CONTRACT_VERSION:
        raise ContractMismatchError(
            f"PostIMAP reports contract_version={info.contract_version} "
            f"(service_version={info.service_version}), but this build was "
            f"written against contract_version={SUPPORTED_CONTRACT_VERSION}. "
            f"Refusing to start against an incompatible contract."
        )


def _parse_service_version(service_version: str) -> tuple[int, ...]:
    """
    Parse a dotted X.Y.Z service version into a comparable tuple.

    Args:
        service_version: e.g. "1.0.1"

    Returns:
        A tuple of ints, or an empty tuple if unparseable (treated as older
        than any real release -- a capability check against it correctly
        reports the capability as unavailable rather than raising)
    """
    parts: list[int] = []
    for piece in service_version.split("."):
        if not piece.isdigit():
            return ()
        parts.append(int(piece))
    return tuple(parts)


def supports_account_delete(info: PostimapVersionInfo) -> bool:
    """
    Whether the running PostIMAP grants DELETE on accounts.

    Args:
        info: Version info read from postimap_info

    Returns:
        True if service_version >= MIN_ACCOUNT_DELETE_SERVICE_VERSION
    """
    return _parse_service_version(info.service_version) >= MIN_ACCOUNT_DELETE_SERVICE_VERSION


def supports_folder_crud(info: PostimapVersionInfo) -> bool:
    """
    Whether the running PostIMAP grants folder creation and deletion.

    Args:
        info: Version info read from postimap_info

    Returns:
        True if service_version >= MIN_FOLDER_CRUD_SERVICE_VERSION
    """
    return _parse_service_version(info.service_version) >= MIN_FOLDER_CRUD_SERVICE_VERSION


def supports_draft_edit(info: PostimapVersionInfo) -> bool:
    """
    Whether the running PostIMAP has outbox.replaces_message_id.

    Args:
        info: Version info read from postimap_info

    Returns:
        True if service_version >= MIN_DRAFT_EDIT_SERVICE_VERSION
    """
    return _parse_service_version(info.service_version) >= MIN_DRAFT_EDIT_SERVICE_VERSION


def supports_sync_notifications(info: PostimapVersionInfo) -> bool:
    """
    Whether the running PostIMAP has the sync_notifications table and grants.

    Args:
        info: Version info read from postimap_info

    Returns:
        True if service_version >= MIN_SYNC_NOTIFICATIONS_SERVICE_VERSION
    """
    return (
        _parse_service_version(info.service_version)
        >= MIN_SYNC_NOTIFICATIONS_SERVICE_VERSION
    )
