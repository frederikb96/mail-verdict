"""
Identity API endpoints.

GET /api/identities — list identities, optionally scoped to one account
POST /api/identities — create an identity
PATCH /api/identities/:id — update address, display name or default status
DELETE /api/identities/:id — delete an identity

An identity is one address a mail account may send as. outbox.from_addr
(PostIMAP-owned) is a free-form string with no notion of which addresses
an account legitimately owns; resolve_send_from_addr() below is what
api/outbox.py and api/mcp_tools.py consult before writing it. Deleting an
identity has no effect on mail already sent -- from_addr is copied onto
the outbox row at insert time, never referenced by id.

At most one identity per account is the default, enforced by a partial
unique index at the database level (see database/models.py's Identity).
An account's first identity is always made the default regardless of
what the request asks for; deleting the default promotes the
next-oldest survivor; an account with none at all resolves to no
override, which is insert_outbox()'s own existing fallback to
accounts.imap_user -- unchanged for any account that never adopts this
table.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.schemas import IdentityCreate, IdentityResponse, IdentityUpdate
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Account, Identity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/identities", tags=["identities"])


def _to_response(identity: Identity) -> IdentityResponse:
    return IdentityResponse(
        id=identity.id,
        account_id=identity.account_id,
        email=identity.email,
        display_name=identity.display_name,
        is_default=identity.is_default,
        created_at=identity.created_at,
    )


async def _clear_default(
    session: AsyncSession, account_id: uuid.UUID, keep: uuid.UUID | None,
) -> None:
    """Unset is_default on every identity of the account except `keep`.

    Always run before setting a new default, never after: the partial
    unique index allows at most one true row at the end of any one
    statement, and unsetting first keeps that true throughout rather than
    relying on both writes landing in the same statement.
    """
    stmt = select(Identity).where(
        Identity.account_id == account_id, Identity.is_default.is_(True),
    )
    if keep is not None:
        stmt = stmt.where(Identity.id != keep)
    result = await session.execute(stmt)
    for identity in result.scalars().all():
        identity.is_default = False
    await session.flush()


@router.get("", response_model=list[IdentityResponse])
async def list_identities(account_id: uuid.UUID | None = None) -> list[IdentityResponse]:
    """List identities, oldest first, optionally scoped to one account."""
    db = get_db_connection()
    async with db.session() as session:
        stmt = select(Identity).order_by(Identity.created_at)
        if account_id is not None:
            stmt = stmt.where(Identity.account_id == account_id)
        result = await session.execute(stmt)
        identities = list(result.scalars().all())
    return [_to_response(identity) for identity in identities]


@router.post("", response_model=IdentityResponse, status_code=201)
async def create_identity(request: IdentityCreate) -> IdentityResponse:
    """Create an identity on an account."""
    db = get_db_connection()
    email = request.email.strip()
    if not email:
        raise HTTPException(status_code=400, detail="email must not be empty")

    async with db.session() as session:
        account = await session.execute(
            select(Account.id).where(Account.id == request.account_id)
        )
        if account.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Account not found")

        has_any = await session.scalar(
            select(Identity.id).where(Identity.account_id == request.account_id).limit(1)
        )
        make_default = request.is_default or has_any is None
        if make_default:
            await _clear_default(session, request.account_id, keep=None)

        identity = Identity(
            account_id=request.account_id,
            email=email,
            display_name=request.display_name,
            is_default=make_default,
        )
        session.add(identity)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"{email!r} is already an identity on this account",
            ) from exc
        await session.refresh(identity)
        return _to_response(identity)


@router.patch("/{identity_id}", response_model=IdentityResponse)
async def update_identity(identity_id: uuid.UUID, request: IdentityUpdate) -> IdentityResponse:
    """Update an identity's address, display name or default status."""
    values = request.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=400, detail="No fields to update")

    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Identity).where(Identity.id == identity_id))
        identity = result.scalar_one_or_none()
        if identity is None:
            raise HTTPException(status_code=404, detail="Identity not found")

        if values.get("is_default") is False and identity.is_default:
            raise HTTPException(
                status_code=400,
                detail="Cannot unset the only default identity; "
                "set another identity as default instead",
            )

        if "email" in values:
            email = (values["email"] or "").strip()
            if not email:
                raise HTTPException(status_code=400, detail="email must not be empty")
            identity.email = email
        if "display_name" in values:
            identity.display_name = values["display_name"]
        if values.get("is_default") is True and not identity.is_default:
            await _clear_default(session, identity.account_id, keep=identity.id)
            identity.is_default = True

        # Captured before flush: a rollback expires every attribute on
        # `identity`, and reading one back afterwards would issue a lazy
        # SELECT outside the greenlet context flush() ran in, raising
        # MissingGreenlet instead of the 409 this is building.
        attempted_email = identity.email
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"{attempted_email!r} is already an identity on this account",
            ) from exc
        await session.refresh(identity)
        return _to_response(identity)


@router.delete("/{identity_id}", status_code=204)
async def delete_identity(identity_id: uuid.UUID) -> None:
    """
    Delete an identity.

    If it was the default and other identities remain on the account, the
    next-oldest survivor is promoted -- an account with any identities at
    all always has exactly one default, both before and after a delete.
    """
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Identity).where(Identity.id == identity_id))
        identity = result.scalar_one_or_none()
        if identity is None:
            raise HTTPException(status_code=404, detail="Identity not found")

        was_default = identity.is_default
        account_id = identity.account_id
        await session.execute(delete(Identity).where(Identity.id == identity_id))

        if was_default:
            successor_id = await session.scalar(
                select(Identity.id)
                .where(Identity.account_id == account_id)
                .order_by(Identity.created_at)
                .limit(1)
            )
            if successor_id is not None:
                await session.execute(
                    update(Identity).where(Identity.id == successor_id).values(is_default=True)
                )


async def resolve_send_from_addr(
    session: AsyncSession,
    account_id: uuid.UUID,
    identity_id: uuid.UUID | None,
) -> str | None:
    """
    Resolve the from_addr an outbox insert should carry.

    identity_id names one explicitly, validated to belong to account_id.
    None falls back to the account's default identity, if it has one; an
    account with no identities at all resolves to None, which
    insert_outbox() already treats as "use accounts.imap_user" -- so a
    caller naming no identity behaves exactly as it did before this table
    existed.

    Raises:
        HTTPException: 404 if identity_id names no identity, 400 if it
            names one belonging to a different account.
    """
    if identity_id is not None:
        result = await session.execute(select(Identity).where(Identity.id == identity_id))
        identity = result.scalar_one_or_none()
        if identity is None:
            raise HTTPException(status_code=404, detail=f"Identity {identity_id} not found")
        if identity.account_id != account_id:
            raise HTTPException(
                status_code=400,
                detail=f"Identity {identity_id} belongs to a different account",
            )
        return identity.email

    default_email: str | None = await session.scalar(
        select(Identity.email).where(
            Identity.account_id == account_id, Identity.is_default.is_(True),
        )
    )
    return default_email
