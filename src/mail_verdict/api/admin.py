"""
Admin routes -- users, and who owns which account.

Every route here requires an admin, and none of them reads mail, events,
contacts or anything else inside an account: an admin manages who exists and
who owns what, by display name, never what they hold.

GET   /api/admin/users                         -- every user, their subjects and account counts
POST  /api/admin/users                         -- add a user before their first sign-in
PATCH /api/admin/users/{user_id}               -- rename, change role, disable or enable
DELETE /api/admin/users/{user_id}              -- remove a user and everything that is theirs
POST  /api/admin/users/{user_id}/subjects      -- let another token subject sign in as a user
DELETE /api/admin/users/{user_id}/subjects/{subject} -- stop a subject signing in as a user
GET   /api/admin/accounts                      -- every mail account and its owner
PUT   /api/admin/accounts/{account_id}/owner   -- give a mail account to another user
GET   /api/admin/dav-accounts                  -- every CalDAV/CardDAV account and its owner
PUT   /api/admin/dav-accounts/{dav_account_id}/owner
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Table, delete, func, select
from sqlalchemy.exc import IntegrityError

from mail_verdict.alerts.audience import reassign_account_alerts
from mail_verdict.api.accounts import remove_mail_account, require_account_delete_support
from mail_verdict.api.dav_accounts import require_dav_support
from mail_verdict.api.deps import require_admin, require_principal
from mail_verdict.api.events import get_event_ring
from mail_verdict.api.sign_ins import revoke_sign_ins
from mail_verdict.api.unified import drop_account_memberships
from mail_verdict.auth.ownership import (
    account_display_name,
    forget_account_owner,
    forget_dav_account_owner,
    owned_account_name,
    owner_of_account,
    set_account_owner,
    set_dav_account_owner,
    user_changed_on_commit,
)
from mail_verdict.auth.principal import OWNER_USER_ID, Principal, get_principal_resolver
from mail_verdict.auth.sign_ins import get_sign_in_registry
from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import (
    Account,
    AccountOwner,
    Base,
    DavAccount,
    DavAccountOwner,
    SignIn,
    User,
    UserIdentity,
)
from mail_verdict.orders.announce import announce_changes
from mail_verdict.orders.ownership import reassign_account
from mail_verdict.postimap.actions import delete_dav_account, update_account, update_dav_account

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class AdminUserResponse(BaseModel):
    """A user as an admin sees them: who, which subjects sign in as them, and
    how many accounts they own -- nothing inside those accounts."""

    id: uuid.UUID
    email: str | None
    display_name: str
    role: Literal["admin", "user"]
    disabled: bool
    is_owner: bool
    subjects: list[str]
    account_count: int
    dav_account_count: int
    created_at: datetime
    last_seen_at: datetime | None


class AdminUserCreateRequest(BaseModel):
    """A user added ahead of their first sign-in, so an account can be given
    to them first. `subject` is the `sub` claim their tokens will carry."""

    subject: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    email: str | None = None
    role: Literal["admin", "user"] = "user"


class AdminUserUpdateRequest(BaseModel):
    """The fields an admin may change on a user."""

    display_name: str | None = Field(default=None, min_length=1)
    role: Literal["admin", "user"] | None = None
    disabled: bool | None = None


class AdminSubjectRequest(BaseModel):
    """A token subject (`sub` claim) to sign in as a user."""

    subject: str = Field(min_length=1)


class AdminAccountResponse(BaseModel):
    """A mail or DAV account as an admin sees it: its name and owner."""

    id: uuid.UUID
    name: str
    owner_user_id: uuid.UUID | None
    is_active: bool
    state: str


class AdminOwnerRequest(BaseModel):
    """The user an account is given to."""

    user_id: uuid.UUID


async def _user_responses(user_id: uuid.UUID | None = None) -> list[AdminUserResponse]:
    """Users with their subjects and account counts -- every one, or one."""
    accounts = (
        select(AccountOwner.user_id, func.count().label("n"))
        .group_by(AccountOwner.user_id)
        .subquery()
    )
    dav_accounts = (
        select(DavAccountOwner.user_id, func.count().label("n"))
        .group_by(DavAccountOwner.user_id)
        .subquery()
    )
    query = (
        select(User, accounts.c.n, dav_accounts.c.n)
        .outerjoin(accounts, accounts.c.user_id == User.id)
        .outerjoin(dav_accounts, dav_accounts.c.user_id == User.id)
        .order_by(User.created_at, User.id)
    )
    if user_id is not None:
        query = query.where(User.id == user_id)
    async with get_db_connection().session() as session:
        rows = (await session.execute(query)).all()
        subjects: dict[uuid.UUID, list[str]] = {}
        for subject, owner in (
            await session.execute(
                select(UserIdentity.subject, UserIdentity.user_id).order_by(UserIdentity.subject)
            )
        ).tuples():
            subjects.setdefault(owner, []).append(subject)
    return [
        AdminUserResponse(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            role="admin" if user.role == "admin" or user.id == OWNER_USER_ID else "user",
            disabled=user.disabled,
            is_owner=user.id == OWNER_USER_ID,
            subjects=subjects.get(user.id, []),
            account_count=account_count or 0,
            dav_account_count=dav_account_count or 0,
            created_at=user.created_at,
            last_seen_at=user.last_seen_at,
        )
        for user, account_count, dav_account_count in rows
    ]


@router.get("/users", response_model=list[AdminUserResponse])
async def list_users() -> list[AdminUserResponse]:
    """Every user."""
    return await _user_responses()


@router.post("/users", response_model=AdminUserResponse, status_code=201)
async def create_user(request: AdminUserCreateRequest) -> AdminUserResponse:
    """Add a user ahead of their first sign-in. 409 if the subject already
    signs in as someone."""
    user_id = uuid.uuid4()
    subject = request.subject
    try:
        async with get_db_connection().session() as session:
            session.add(
                User(
                    id=user_id, email=request.email, display_name=request.display_name,
                    role=request.role,
                )
            )
            await session.flush()
            session.add(UserIdentity(subject=subject, user_id=user_id))
            await session.flush()
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="subject already belongs to a user") from exc
    resolver = get_principal_resolver()
    if resolver is not None:
        resolver.forget()
    return (await _user_responses(user_id))[0]


@router.patch("/users/{user_id}", response_model=AdminUserResponse)
async def update_user(user_id: uuid.UUID, request: AdminUserUpdateRequest) -> AdminUserResponse:
    """Rename a user, change their role, or disable or enable them. The owner
    can be neither demoted nor disabled -- they are an enabled admin
    regardless (auth/principal.py), so either change would only make this
    table lie."""
    values = request.model_dump(exclude_unset=True, exclude_none=True)
    if not values:
        raise HTTPException(status_code=400, detail="No fields to update")
    if user_id == OWNER_USER_ID and (values.get("role") == "user" or values.get("disabled")):
        raise HTTPException(status_code=400, detail="the owner is always an enabled admin")
    async with get_db_connection().session() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        for name, value in values.items():
            setattr(user, name, value)
        user_changed_on_commit(session)
    resolver = get_principal_resolver()
    if resolver is not None:
        resolver.forget(user_id)
    return (await _user_responses(user_id))[0]


def _user_owned_tables() -> list[Table]:
    """Every table with a foreign key onto users.id, dependents first --
    derived from the models, so a table added for users is covered by
    remove_user without being listed anywhere."""
    users = Base.metadata.tables["users"]
    return [
        table for table in reversed(Base.metadata.sorted_tables)
        if any(fk.column.table is users for fk in table.foreign_keys)
    ]


@router.delete("/users/{user_id}", status_code=204)
async def remove_user(
    user_id: uuid.UUID, principal: Principal = Depends(require_principal),
) -> None:
    """
    Remove a user completely, in one transaction: their mail accounts (as
    deleting each one does: its orders, then the account and its mirrored
    mailbox), their calendar and contact accounts, and then every row that
    names them -- orders, files, rules, views, settings, alerts, push
    devices, sign-ins and subjects -- and the user. Nothing on any mail or
    DAV server is touched. Their tokens are refused from then on, as an
    unknown subject's (with auth.auto_provision on, a later sign-in starts
    an empty new user).

    400 for the owner or the caller themselves; 409 while the glacier holds
    mail of one of their accounts; 501 if PostIMAP cannot delete what they
    own.
    """
    if user_id == OWNER_USER_ID:
        raise HTTPException(status_code=400, detail="the owner cannot be removed")
    if user_id == principal.user_id:
        raise HTTPException(status_code=400, detail="you cannot remove yourself")
    async with get_db_connection().session() as session:
        if await session.get(User, user_id) is None:
            raise HTTPException(status_code=404, detail="User not found")
        accounts = (
            await session.execute(
                select(AccountOwner.account_id).where(AccountOwner.user_id == user_id)
            )
        ).scalars().all()
        dav_accounts = (
            await session.execute(
                select(DavAccountOwner.dav_account_id).where(DavAccountOwner.user_id == user_id)
            )
        ).scalars().all()
        if accounts:
            await require_account_delete_support(session)
        if dav_accounts:
            await require_dav_support()
        for account_id in accounts:
            if await session.get(Account, account_id) is None:
                await forget_account_owner(session, account_id)
            else:
                await remove_mail_account(session, account_id)
        for dav_account_id in dav_accounts:
            await forget_dav_account_owner(session, dav_account_id)
            await delete_dav_account(session, dav_account_id)
        for table in _user_owned_tables():
            await session.execute(delete(table).where(table.c.user_id == user_id))
        await session.execute(delete(User).where(User.id == user_id))
        user_changed_on_commit(session)
    resolver = get_principal_resolver()
    if resolver is not None:
        resolver.forget()
    registry = get_sign_in_registry()
    if registry is not None:
        registry.forget()


@router.post("/users/{user_id}/subjects", response_model=AdminUserResponse, status_code=201)
async def add_subject(user_id: uuid.UUID, request: AdminSubjectRequest) -> AdminUserResponse:
    """Let another token subject sign in as this user -- a second login, or
    a machine client's. 409 if the subject already signs in as someone."""
    try:
        async with get_db_connection().session() as session:
            if await session.get(User, user_id) is None:
                raise HTTPException(status_code=404, detail="User not found")
            session.add(UserIdentity(subject=request.subject, user_id=user_id))
            await session.flush()
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="subject already belongs to a user") from exc
    resolver = get_principal_resolver()
    if resolver is not None:
        resolver.forget()
    return (await _user_responses(user_id))[0]


@router.delete("/users/{user_id}/subjects/{subject}", response_model=AdminUserResponse)
async def remove_subject(
    user_id: uuid.UUID, subject: str, principal: Principal = Depends(require_principal),
) -> AdminUserResponse:
    """Stop a token subject signing in as this user -- the owner's included
    -- and revoke the phones and machine clients recorded for it. 404 if it
    is not this user's; 400 for the subject this request signed in with,
    which would lock the caller out mid-request."""
    if user_id == principal.user_id and subject == principal.subject:
        raise HTTPException(
            status_code=400, detail="this is the subject you are signed in with",
        )
    async with get_db_connection().session() as session:
        identity = await session.get(UserIdentity, subject)
        if identity is None or identity.user_id != user_id:
            raise HTTPException(status_code=404, detail="Subject not found for this user")
        await session.delete(identity)
        user_changed_on_commit(session)
    await revoke_sign_ins(SignIn.subject == subject, SignIn.kind != "browser")
    resolver = get_principal_resolver()
    if resolver is not None:
        resolver.forget()
    return (await _user_responses(user_id))[0]


@router.get("/accounts", response_model=list[AdminAccountResponse])
async def list_accounts() -> list[AdminAccountResponse]:
    """Every mail account, by display name, with its owner."""
    async with get_db_connection().session() as session:
        rows = (
            await session.execute(
                select(Account.id, Account.name, Account.is_active, Account.state,
                       AccountOwner.user_id)
                .outerjoin(AccountOwner, AccountOwner.account_id == Account.id)
                .order_by(Account.name)
            )
        ).all()
    return [
        AdminAccountResponse(
            id=row.id, name=account_display_name(row.name), owner_user_id=row.user_id,
            is_active=row.is_active, state=row.state,
        )
        for row in rows
    ]


@router.get("/dav-accounts", response_model=list[AdminAccountResponse])
async def list_dav_accounts() -> list[AdminAccountResponse]:
    """Every CalDAV/CardDAV account, by display name, with its owner."""
    async with get_db_connection().session() as session:
        rows = (
            await session.execute(
                select(DavAccount.id, DavAccount.name, DavAccount.is_active, DavAccount.state,
                       DavAccountOwner.user_id)
                .outerjoin(DavAccountOwner, DavAccountOwner.dav_account_id == DavAccount.id)
                .order_by(DavAccount.name)
            )
        ).all()
    return [
        AdminAccountResponse(
            id=row.id, name=account_display_name(row.name), owner_user_id=row.user_id,
            is_active=row.is_active, state=row.state,
        )
        for row in rows
    ]


@router.put("/accounts/{account_id}/owner", response_model=AdminAccountResponse)
async def set_owner(
    account_id: uuid.UUID, request: AdminOwnerRequest,
) -> AdminAccountResponse:
    """Give a mail account to another user. Its stored name moves into the
    new owner's namespace in the same transaction; 409 if they already have
    an account by that name. Its orders (orders/ownership.py) and its alerts
    (alerts/audience.py) follow, and its folders leave the previous owner's unified
    views."""
    new_owner = request.user_id
    try:
        async with get_db_connection().session() as session:
            if await session.get(User, new_owner) is None:
                raise HTTPException(status_code=404, detail="User not found")
            account = await session.get(Account, account_id)
            if account is None:
                raise HTTPException(status_code=404, detail="Account not found")
            name = owned_account_name(new_owner, account_display_name(account.name))
            await update_account(session, account_id, name=name)
            previous_owner = await owner_of_account(session, account_id)
            await set_account_owner(session, account_id, new_owner)
            order_changes = await reassign_account(session, account_id, new_owner)
            await reassign_account_alerts(session, account_id, new_owner)
            if previous_owner != new_owner:
                await drop_account_memberships(session, account_id)
            response = AdminAccountResponse(
                id=account_id, name=account_display_name(name), owner_user_id=new_owner,
                is_active=account.is_active, state=account.state,
            )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="that user already has an account by this name",
        ) from exc
    await announce_changes(get_event_ring(), order_changes)
    return response


@router.put("/dav-accounts/{dav_account_id}/owner", response_model=AdminAccountResponse)
async def set_dav_owner(
    dav_account_id: uuid.UUID, request: AdminOwnerRequest,
) -> AdminAccountResponse:
    """Give a CalDAV/CardDAV account to another user, as set_owner does for mail."""
    new_owner = request.user_id
    try:
        async with get_db_connection().session() as session:
            if await session.get(User, new_owner) is None:
                raise HTTPException(status_code=404, detail="User not found")
            account = await session.get(DavAccount, dav_account_id)
            if account is None:
                raise HTTPException(status_code=404, detail="DAV account not found")
            name = owned_account_name(new_owner, account_display_name(account.name))
            await update_dav_account(session, dav_account_id, name=name)
            await set_dav_account_owner(session, dav_account_id, new_owner)
            response = AdminAccountResponse(
                id=dav_account_id, name=account_display_name(name), owner_user_id=new_owner,
                is_active=account.is_active, state=account.state,
            )
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409, detail="that user already has a DAV account by this name",
        ) from exc
    return response
