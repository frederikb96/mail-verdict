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
