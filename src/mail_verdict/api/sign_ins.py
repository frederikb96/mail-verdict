"""
Signed-in phones, browsers and machine clients (auth/sign_ins.py), and
revoking them.

Anyone sees and revokes their own; an admin any one, and every sign-in of a
person at once. Revoking keeps the row, so the same sign-in stays refused;
a phone signed in again is a new row, and a person's browsers come back once
they sign in again at the identity provider.

GET  /api/me/sign-ins                          -- the caller's sign-ins
POST /api/me/sign-ins/{sign_in_id}/revoke      -- revoke one of them
GET  /api/admin/sign-ins                       -- every sign-in (?user_id=, ?kind=)
POST /api/admin/sign-ins/{sign_in_id}/revoke   -- revoke any one
POST /api/admin/users/{user_id}/sign-out       -- revoke every sign-in of a user
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import ColumnElement, func, select, update

from mail_verdict.api.deps import require_admin, require_principal
from mail_verdict.auth.ownership import user_changed_on_commit
from mail_verdict.auth.principal import Principal
from mail_verdict.auth.sign_ins import get_sign_in_registry
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import SignIn, User

me_router = APIRouter(prefix="/me/sign-ins", tags=["sign-ins"])
admin_router = APIRouter(prefix="/admin", tags=["sign-ins"], dependencies=[Depends(require_admin)])


class SignInResponse(BaseModel):
    """One sign-in. `name` is what a phone or client calls itself
    (X-Device-Name); `current` is the sign-in making this request."""

    id: uuid.UUID
    user_id: uuid.UUID
    kind: Literal["phone", "browser", "machine"]
    name: str | None
    subject: str
    first_seen_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None
    current: bool


class SignOutResponse(BaseModel):
    """How many sign-ins a sign-out revoked."""

    revoked: int


def _response(row: SignIn, principal: Principal) -> SignInResponse:
    return SignInResponse(
        id=row.id, user_id=row.user_id, kind=row.kind,  # type: ignore[arg-type]
        name=row.name, subject=row.subject, first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at, revoked_at=row.revoked_at,
        current=row.id == principal.sign_in_id,
    )


async def _list(principal: Principal, *conditions: ColumnElement[bool]) -> list[SignInResponse]:
    async with get_db_connection().session() as session:
        rows = (
            await session.execute(
                select(SignIn).where(*conditions).order_by(
                    SignIn.revoked_at.is_not(None), SignIn.last_seen_at.desc(), SignIn.id,
                )
            )
        ).scalars().all()
    return [_response(row, principal) for row in rows]


async def revoke_sign_ins(*conditions: ColumnElement[bool]) -> int:
    """
    Revoke every unrevoked sign-in matching `conditions`, end their open
    event streams and drop what this replica remembers about them.

    Returns:
        How many were revoked.
    """
    async with get_db_connection().session() as session:
        result = await session.execute(
            update(SignIn)
            .where(*conditions, SignIn.revoked_at.is_(None))
            .values(revoked_at=func.now())
        )
        revoked = int(result.rowcount or 0)  # type: ignore[attr-defined]
        if revoked:
            user_changed_on_commit(session)
    registry = get_sign_in_registry()
    if registry is not None:
        registry.forget()
    return revoked


async def _revoke_one(sign_in_id: uuid.UUID, principal: Principal, *, own: bool) -> SignInResponse:
    async with get_db_connection().session() as session:
        row = await session.get(SignIn, sign_in_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Sign-in not found")
        if own and row.user_id != principal.user_id:
            raise HTTPException(status_code=403, detail="Not your sign-in")
    await revoke_sign_ins(SignIn.id == sign_in_id)
    async with get_db_connection().session() as session:
        row = await session.get(SignIn, sign_in_id)
        assert row is not None
        return _response(row, principal)


@me_router.get("", response_model=list[SignInResponse])
async def list_own_sign_ins(
    principal: Principal = Depends(require_principal),
) -> list[SignInResponse]:
    """The caller's sign-ins, live ones first, most recently seen first."""
    return await _list(principal, SignIn.user_id == principal.user_id)


@me_router.post("/{sign_in_id}/revoke", response_model=SignInResponse)
async def revoke_own_sign_in(
    sign_in_id: uuid.UUID, principal: Principal = Depends(require_principal),
) -> SignInResponse:
    """Revoke one of the caller's sign-ins -- the current one included,
    whose next request is then refused. 403 for someone else's."""
    return await _revoke_one(sign_in_id, principal, own=True)


@admin_router.get("/sign-ins", response_model=list[SignInResponse])
async def list_sign_ins(
    user_id: uuid.UUID | None = Query(default=None),
    kind: Literal["phone", "browser", "machine"] | None = Query(default=None),
    principal: Principal = Depends(require_principal),
) -> list[SignInResponse]:
    """Every sign-in, or one user's, or one kind's (`machine` is the list of
    access keys)."""
    conditions: list[ColumnElement[bool]] = []
    if user_id is not None:
        conditions.append(SignIn.user_id == user_id)
    if kind is not None:
        conditions.append(SignIn.kind == kind)
    return await _list(principal, *conditions)


@admin_router.post("/sign-ins/{sign_in_id}/revoke", response_model=SignInResponse)
async def revoke_sign_in(
    sign_in_id: uuid.UUID, principal: Principal = Depends(require_principal),
) -> SignInResponse:
    """Revoke any sign-in."""
    return await _revoke_one(sign_in_id, principal, own=False)


@admin_router.post("/users/{user_id}/sign-out", response_model=SignOutResponse)
async def sign_out_user(user_id: uuid.UUID) -> SignOutResponse:
    """Revoke every sign-in of a user: their phones, their browsers and
    their machine clients. Their phones and browsers can sign in again; a
    machine client's subject stays refused."""
    async with get_db_connection().session() as session:
        if await session.get(User, user_id) is None:
            raise HTTPException(status_code=404, detail="User not found")
    return SignOutResponse(revoked=await revoke_sign_ins(SignIn.user_id == user_id))
