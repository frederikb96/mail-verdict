"""
PATCH .../folders/{folder_id}/prefs against the glacier's own synthetic
folder id -- visibility, display name and unified-view membership are
MailVerdict-owned columns the same as for a real folder, so a request
touching only those must not 404 just because there is no row for this
id in `folders` (design section 2: the glacier folder id is synthetic).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from mail_verdict.api.folder_management import update_folder_prefs
from mail_verdict.api.schemas import FolderPrefsUpdate
from mail_verdict.database.connection import DatabaseConnection


async def _seed_account_with_glacier(session: AsyncSession) -> tuple[uuid.UUID, uuid.UUID]:
    account_id = uuid.uuid4()
    glacier_folder_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO accounts "
            "(id, name, imap_host, imap_port, imap_user, imap_password) "
            "VALUES (:id, :name, 'imap.example.com', 993, 'user@example.com', "
            "'\\x00' || convert_to('pw', 'UTF8'))"
        ),
        {"id": account_id, "name": f"acct-{account_id}"},
    )
    await session.execute(
        text(
            "INSERT INTO account_prefs (account_id, glacier_enabled, glacier_folder_id) "
            "VALUES (:account_id, true, :glacier_folder_id)"
        ),
        {"account_id": account_id, "glacier_folder_id": glacier_folder_id},
    )
    return account_id, glacier_folder_id


@pytest.mark.asyncio
async def test_visibility_updates_on_the_glacier_folder(migrated_db: DatabaseConnection) -> None:
    async with migrated_db.session() as session:
        _account_id, glacier_folder_id = await _seed_account_with_glacier(session)
        await session.commit()

    response = await update_folder_prefs(
        glacier_folder_id, FolderPrefsUpdate(is_visible=False),
    )
    assert response.is_visible is False
    assert response.kind == "glacier"


@pytest.mark.asyncio
async def test_unified_view_membership_updates_on_the_glacier_folder(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        _account_id, glacier_folder_id = await _seed_account_with_glacier(session)
        view_id = uuid.uuid4()
        await session.execute(
            text("INSERT INTO unified_views (id, name) VALUES (:id, 'Everything')"),
            {"id": view_id},
        )
        await session.commit()

    response = await update_folder_prefs(
        glacier_folder_id, FolderPrefsUpdate(unified_view_ids=[view_id]),
    )
    assert response.unified_view_ids == [view_id]


@pytest.mark.asyncio
async def test_real_time_on_the_glacier_folder_is_still_refused(
    migrated_db: DatabaseConnection,
) -> None:
    async with migrated_db.session() as session:
        _account_id, glacier_folder_id = await _seed_account_with_glacier(session)
        await session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await update_folder_prefs(glacier_folder_id, FolderPrefsUpdate(real_time=True))
    assert exc_info.value.status_code == 409
