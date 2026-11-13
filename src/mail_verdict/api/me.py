"""
GET /api/me -- who the caller is.

What a client reads once it holds a token: which user it signed in as and
whether that user is admin, which decides whether admin screens are offered.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from mail_verdict.api.deps import require_principal
from mail_verdict.auth.principal import Principal
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import User

router = APIRouter(tags=["auth"])


class MeResponse(BaseModel):
    """The signed-in user. `role` is the effective one: the owner is admin
    whatever the users table says."""

    user_id: uuid.UUID
    email: str | None
    display_name: str
    role: Literal["admin", "user"]
    disabled: bool


@router.get("/me", response_model=MeResponse)
async def get_me(principal: Principal = Depends(require_principal)) -> MeResponse:
    """The signed-in user."""
    async with get_db_connection().session() as session:
        user = await session.get(User, principal.user_id)
    return MeResponse(
        user_id=principal.user_id,
        email=user.email if user else None,
        display_name=user.display_name if user else principal.subject,
        role="admin" if principal.is_admin else "user",
        disabled=False,
    )
