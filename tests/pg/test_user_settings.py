"""
Per-user settings against a real database: user_settings rows written and
read back through SettingsService, one user's values never another's, and
the instance's admin keys unaffected.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from mail_verdict.auth.principal import OWNER_USER_ID
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.settings.defaults import SETTING_DEFAULTS
from mail_verdict.settings.service import AdminSettingError, SettingsService


async def _add_user(db: DatabaseConnection) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with db.session() as session:
        await session.execute(
            text("INSERT INTO users (id, display_name, role) VALUES (:id, 'U', 'user')"),
            {"id": user_id},
        )
    return user_id


async def _stored(db: DatabaseConnection, user_id: uuid.UUID, category: str) -> dict[str, object]:
    async with db.session() as session:
        row = await session.scalar(
            text("SELECT data FROM user_settings WHERE user_id = :u AND category = :c"),
            {"u": user_id, "c": category},
        )
    return dict(row or {})


@pytest.mark.asyncio
async def test_a_user_value_persists_and_is_read_after_a_restart(
    migrated_db: DatabaseConnection,
) -> None:
    user = await _add_user(migrated_db)
    service = SettingsService(migrated_db)
    await service.load()

    await service.update_for_user("outbox", user, {"undo_send_seconds": 12.0}, is_admin=False)
    restarted = SettingsService(migrated_db)
    await restarted.load()

    assert await _stored(migrated_db, user, "outbox") == {"undo_send_seconds": 12.0}
    assert restarted.get_for_user("outbox", user)["undo_send_seconds"] == 12.0


@pytest.mark.asyncio
async def test_two_users_keep_their_own_values_and_keys_merge_within_one(
    migrated_db: DatabaseConnection,
) -> None:
    first, second = await _add_user(migrated_db), await _add_user(migrated_db)
    service = SettingsService(migrated_db)
    await service.load()

    await service.update_for_user(
        "calendar", first, {"default_reminder_minutes": 5}, is_admin=False,
    )
    await service.update_for_user(
        "calendar", first, {"default_event_duration_minutes": 90}, is_admin=False,
    )
    await service.update_for_user(
        "calendar", second, {"default_reminder_minutes": 60}, is_admin=False,
    )

    one = service.get_for_user("calendar", first)
    two = service.get_for_user("calendar", second)
    assert (one["default_reminder_minutes"], one["default_event_duration_minutes"]) == (5, 90)
    assert two["default_reminder_minutes"] == 60
    assert two["default_event_duration_minutes"] == (
        SETTING_DEFAULTS["calendar"]["default_event_duration_minutes"]
    )


@pytest.mark.asyncio
async def test_a_refused_admin_key_leaves_no_row_behind(
    migrated_db: DatabaseConnection,
) -> None:
    user = await _add_user(migrated_db)
    service = SettingsService(migrated_db)
    await service.load()

    with pytest.raises(AdminSettingError):
        await service.update_for_user(
            "orders", user, {"language": "Dutch", "decide_model": "x"}, is_admin=False,
        )

    assert await _stored(migrated_db, user, "orders") == {}


@pytest.mark.asyncio
async def test_an_admin_write_splits_between_the_instance_and_their_own_row(
    migrated_db: DatabaseConnection,
) -> None:
    service = SettingsService(migrated_db)
    await service.load()
    stranger = await _add_user(migrated_db)

    try:
        await service.update_for_user(
            "orders", OWNER_USER_ID, {"language": "Dutch", "write_model": "split-test"},
            is_admin=True,
        )

        own = await _stored(migrated_db, OWNER_USER_ID, "orders")
        assert own["language"] == "Dutch"
        assert "write_model" not in own
        assert service.get("orders")["write_model"] == "split-test"
        assert service.get_for_user("orders", stranger)["language"] == "English"
        assert service.get_for_user("orders", stranger)["write_model"] == "split-test"
    finally:
        # The database is shared by the whole pg invocation.
        await service.update_for_user(
            "orders", OWNER_USER_ID, {"language": "English", "write_model": ""}, is_admin=True,
        )
