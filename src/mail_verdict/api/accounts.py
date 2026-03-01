"""
Account API endpoints.

GET /api/accounts — list all accounts
POST /api/accounts — create account
PUT /api/accounts/:id — update account
DELETE /api/accounts/:id — delete account
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import delete, select, update

from mail_verdict.api.schemas import (
    AccountCreateRequest,
    AccountResponse,
    AccountUpdateRequest,
    FolderResponse,
)
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import Account, Folder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


@router.get("", response_model=list[AccountResponse])
async def list_accounts() -> list[AccountResponse]:
    """List all configured accounts."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account).order_by(Account.name))
        accounts = list(result.scalars().all())
    return [AccountResponse.model_validate(a) for a in accounts]


@router.post("", response_model=AccountResponse, status_code=201)
async def create_account(request: AccountCreateRequest) -> AccountResponse:
    """Create a new IMAP account."""
    db = get_db_connection()
    account = Account(
        name=request.name,
        imap_host=request.imap_host,
        imap_port=request.imap_port,
        imap_user=request.imap_user,
        smtp_host=request.smtp_host,
        smtp_port=request.smtp_port,
        smtp_user=request.smtp_user,
        is_active=request.is_active,
    )
    async with db.session() as session:
        session.add(account)
        await session.flush()
        await session.refresh(account)
        return AccountResponse.model_validate(account)


@router.put("/{account_id}", response_model=AccountResponse)
async def update_account(
    account_id: uuid.UUID,
    request: AccountUpdateRequest,
) -> AccountResponse:
    """Update an existing account."""
    db = get_db_connection()
    update_values = request.model_dump(exclude_unset=True)
    if not update_values:
        raise HTTPException(status_code=400, detail="No fields to update")

    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        account = result.scalar_one_or_none()
        if account is None:
            raise HTTPException(status_code=404, detail="Account not found")

        await session.execute(
            update(Account).where(Account.id == account_id).values(**update_values)
        )
        await session.refresh(account)
        return AccountResponse.model_validate(account)


@router.delete("/{account_id}", status_code=204)
async def delete_account(account_id: uuid.UUID) -> None:
    """Delete an account and all its data (cascading)."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(select(Account).where(Account.id == account_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Account not found")

        await session.execute(delete(Account).where(Account.id == account_id))


@router.get("/{account_id}/folders", response_model=list[FolderResponse])
async def list_folders(account_id: uuid.UUID) -> list[FolderResponse]:
    """List all folders for an account."""
    db = get_db_connection()
    async with db.session() as session:
        result = await session.execute(
            select(Folder).where(Folder.account_id == account_id).order_by(Folder.imap_name)
        )
        folders = list(result.scalars().all())

    return [
        FolderResponse(
            id=f.id,
            account_id=f.account_id,
            imap_name=f.imap_name,
            display_name=f.display_name,
            special_use=f.special_use.value if f.special_use else None,
            subscribed=f.subscribed,
            last_synced_at=f.last_synced_at,
        )
        for f in folders
    ]
