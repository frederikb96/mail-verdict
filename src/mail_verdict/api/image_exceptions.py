"""
Image exception API endpoints.

GET /api/accounts/:id/image-exceptions — list exceptions
POST /api/accounts/:id/image-exceptions — create exception
DELETE /api/accounts/:id/image-exceptions/:exc_id — delete exception
GET /api/accounts/:id/image-exceptions/check — check if sender is allowed
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import delete, select

from mail_verdict.api.schemas import (
    ImageExceptionCreate,
    ImageExceptionResponse,
)
from mail_verdict.core.image_sanitizer import extract_sender_domain, extract_sender_email
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Account, ImageException, ImageExceptionType

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/accounts/{account_id}/image-exceptions", tags=["image-exceptions"])


@router.get("", response_model=list[ImageExceptionResponse])
async def list_image_exceptions(
    account_id: uuid.UUID,
) -> list[ImageExceptionResponse]:
    """List all image exceptions for an account."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(ImageException)
            .where(ImageException.account_id == account_id)
            .order_by(ImageException.created_at.desc())
        )
        exceptions = list(result.scalars().all())
    return [
        ImageExceptionResponse(
            id=exc.id,
            type=exc.exception_type.value,
            value=exc.value,
            created_at=exc.created_at,
        )
        for exc in exceptions
    ]


@router.post("", response_model=ImageExceptionResponse, status_code=201)
async def create_image_exception(
    account_id: uuid.UUID,
    request: ImageExceptionCreate,
) -> ImageExceptionResponse:
    """Create an image loading exception (sender or domain allowlist entry)."""
    db = get_db_connection()
    async with db.session() as session:
        # Verify account exists
        acct = await session.execute(select(Account).where(Account.id == account_id))
        if acct.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Account not found")

        exc_type = ImageExceptionType(request.type)
        value = request.value.lower().strip()

        exc = ImageException(
            account_id=account_id,
            exception_type=exc_type,
            value=value,
        )
        session.add(exc)
        await session.flush()
        await session.refresh(exc)

        return ImageExceptionResponse(
            id=exc.id,
            type=exc.exception_type.value,
            value=exc.value,
            created_at=exc.created_at,
        )


@router.delete("/{exception_id}", status_code=204)
async def delete_image_exception(
    account_id: uuid.UUID,
    exception_id: uuid.UUID,
) -> None:
    """Delete an image exception."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(ImageException).where(
                ImageException.id == exception_id,
                ImageException.account_id == account_id,
            )
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Image exception not found")

        await session.execute(
            delete(ImageException).where(ImageException.id == exception_id)
        )


@router.get("/check")
async def check_image_exception(
    account_id: uuid.UUID,
    sender: str = Query(description="Sender email address to check"),
) -> dict[str, bool | str | None]:
    """Check if a sender's images are allowed by exceptions."""
    db = get_db_connection()
    email = extract_sender_email(sender)
    domain = extract_sender_domain(sender)

    async with db.session() as session:
        result = await session.execute(
            select(ImageException).where(
                ImageException.account_id == account_id,
            )
        )
        exceptions = list(result.scalars().all())

    for exc in exceptions:
        if exc.exception_type == ImageExceptionType.SENDER and email and exc.value == email:
            return {"allowed": True, "matched_by": f"sender:{exc.value}"}
        if exc.exception_type == ImageExceptionType.DOMAIN and domain and exc.value == domain:
            return {"allowed": True, "matched_by": f"domain:{exc.value}"}

    return {"allowed": False, "matched_by": None}
