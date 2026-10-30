"""
Where the bytes of a document come from: a mail attachment (PostIMAP's
table) or a file added to the register by hand. These two reads are the only
ones in the orders code outside the download routes; every caller names one
row by id, so a list query can never carry bytes along.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.database.models import Attachment, OrderFile


async def read_mail_attachment(session: AsyncSession, attachment_id: uuid.UUID) -> bytes | None:
    """The attachment's bytes, or None when the row or its data is gone."""
    result = await session.execute(select(Attachment.data).where(Attachment.id == attachment_id))
    return result.scalar_one_or_none()


async def read_order_file(session: AsyncSession, file_id: uuid.UUID) -> bytes | None:
    """The uploaded file's bytes, or None when the row is gone."""
    result = await session.execute(select(OrderFile.data).where(OrderFile.id == file_id))
    return result.scalar_one_or_none()
