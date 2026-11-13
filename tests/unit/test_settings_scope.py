"""
Who owns each setting: the instance (admin) or each user -- the tags in
SETTING_SCOPE, the two views of a category in SettingsService, and the
settings routes a non-admin reaches.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mail_verdict.settings.defaults import (
    SETTING_DEFAULTS,
    SETTING_SCOPE,
    SettingCategory,
    schema,
    setting_scope,
    user_keys,
)
from mail_verdict.settings.service import AdminSettingError, SettingsService
from tests.helpers.auth import act_as, owner_scope, user_scope

# What a person sets for themselves. Written out so that moving a key
# between the two sides is a deliberate edit of this test as well.
USER_KEYS = {
    "calendar": {"default_event_duration_minutes", "default_calendar_id",
                 "default_reminder_minutes"},
    "outbox": {"undo_send_seconds"},
    "mail": {"mark_read_on_file_to_archive_or_junk", "bell_badge_counts_new_mail",
             "notify_wait_seconds", "account_order"},
    "orders": {"language", "filter", "auto_close_days", "auto_close_grace_days"},
}


class _MemoryRepo:
    """SettingsRepository's surface over dicts."""

    def __init__(self) -> None:
        self.instance: dict[str, dict[str, Any]] = {}
        self.users: dict[uuid.UUID, dict[str, dict[str, Any]]] = {}

    async def get_all(self) -> dict[str, dict[str, Any]]:
        return {c: dict(d) for c, d in self.instance.items()}

    async def get_category(self, category: str) -> dict[str, Any] | None:
        return dict(self.instance[category]) if category in self.instance else None

    async def upsert_category(self, category: str, data: dict[str, Any]) -> None:
        self.instance[category] = {**self.instance.get(category, {}), **data}

    async def get_all_user_settings(self) -> dict[uuid.UUID, dict[str, dict[str, Any]]]:
        return {u: {c: dict(d) for c, d in cats.items()} for u, cats in self.users.items()}

    async def upsert_user_category(
        self, user_id: uuid.UUID, category: str, data: dict[str, Any],
    ) -> dict[str, Any]:
        cats = self.users.setdefault(user_id, {})
        cats[category] = {**cats.get(category, {}), **data}
        return dict(cats[category])


async def _service() -> tuple[SettingsService, _MemoryRepo]:
    service = SettingsService(MagicMock())
    repo = _MemoryRepo()
    service._repo = repo  # type: ignore[assignment]
    await service.load()
    return service, repo


class TestEveryKeyIsTagged:
    def test_every_default_key_has_a_scope(self) -> None:
        for category, defaults in SETTING_DEFAULTS.items():
            tagged = SETTING_SCOPE.get(category, {})
            assert set(tagged) == set(defaults), (
                f"{category}: tag every key admin or user in SETTING_SCOPE "
                f"(untagged: {sorted(set(defaults) - set(tagged))}, "
                f"tagged but gone: {sorted(set(tagged) - set(defaults))})"
            )
            assert set(tagged.values()) <= {"admin", "user"}

    def test_every_category_is_covered(self) -> None:
        assert {c.value for c in SettingCategory} == {c.value for c in SETTING_DEFAULTS}

    def test_the_user_keys_are_exactly_the_personal_preferences(self) -> None:
        actual = {
            category.value: set(user_keys(category.value))
            for category in SettingCategory if user_keys(category.value)
        }
        assert actual == USER_KEYS

    def test_a_key_nobody_tagged_is_admin(self) -> None:
        assert setting_scope("mail", "something_new") == "admin"
        assert setting_scope("no_such_category", "x") == "admin"

    def test_the_schema_names_every_key_with_scope_and_type(self) -> None:
        described = schema()
        assert set(described) == {c.value for c in SettingCategory}
        assert described["outbox"]["undo_send_seconds"] == {"scope": "user", "type": "number"}
        assert described["ai"]["model"] == {"scope": "admin", "type": "string"}
        assert described["mail"]["account_order"] == {"scope": "user", "type": "array"}
        assert described["orders"]["filter"]["type"] == "object"
        assert described["ai"]["base_url"]["type"] == "any"


class TestSettingsServiceViews:
    @pytest.mark.asyncio
    async def test_a_user_key_is_per_user(self) -> None:
        service, _ = await _service()
        alice, bob = uuid.uuid4(), uuid.uuid4()

        await service.update_for_user("outbox", alice, {"undo_send_seconds": 20.0}, is_admin=False)

        assert service.get_for_user("outbox", alice)["undo_send_seconds"] == 20.0
        default = SETTING_DEFAULTS["outbox"]["undo_send_seconds"]
        assert service.get_for_user("outbox", bob)["undo_send_seconds"] == default

    @pytest.mark.asyncio
    async def test_a_non_admin_cannot_write_an_admin_key_and_nothing_is_written(self) -> None:
        service, repo = await _service()
        alice = uuid.uuid4()

        with pytest.raises(AdminSettingError):
            await service.update_for_user(
                "orders", alice, {"language": "German", "decide_model": "x"}, is_admin=False,
            )

        assert repo.instance == {} and repo.users == {}
        assert service.get_for_user("orders", alice)["language"] == "English"

    @pytest.mark.asyncio
    async def test_a_non_admin_cannot_write_a_key_that_is_not_a_setting(self) -> None:
        service, _ = await _service()

        with pytest.raises(AdminSettingError):
            await service.update_for_user("mail", uuid.uuid4(), {"made_up": 1}, is_admin=False)

    @pytest.mark.asyncio
    async def test_an_admin_write_splits_by_key(self) -> None:
        service, repo = await _service()
        admin = uuid.uuid4()

        await service.update_for_user(
            "orders", admin, {"language": "German", "decide_model": "m"}, is_admin=True,
        )

        assert repo.instance["orders"] == {"decide_model": "m"}
        assert repo.users[admin]["orders"] == {"language": "German"}
        view = service.get_for_user("orders", admin)
        assert (view["language"], view["decide_model"]) == ("German", "m")

    @pytest.mark.asyncio
    async def test_instance_values_of_user_keys_never_reach_a_user(self) -> None:
        service, repo = await _service()
        repo.instance["mail"] = {"bell_badge_counts_new_mail": False}
        await service.load()

        assert service.get_for_user("mail", uuid.uuid4())["bell_badge_counts_new_mail"] is True

    @pytest.mark.asyncio
    async def test_a_user_row_holding_an_admin_key_is_ignored(self) -> None:
        service, repo = await _service()
        alice = uuid.uuid4()
        repo.users[alice] = {"orders": {"decide_model": "sneaky", "language": "French"}}
        await service.load()

        view = service.get_for_user("orders", alice)

        assert view["decide_model"] == SETTING_DEFAULTS["orders"]["decide_model"]
        assert view["language"] == "French"

    @pytest.mark.asyncio
    async def test_the_user_keys_view_holds_only_user_keys(self) -> None:
        service, _ = await _service()

        assert set(service.get_user_keys("orders", uuid.uuid4())) == USER_KEYS["orders"]

    @pytest.mark.asyncio
    async def test_system_work_reads_the_defaults_of_user_keys(self) -> None:
        service, _ = await _service()

        assert service.get_for_user("outbox", None) == SETTING_DEFAULTS["outbox"]

    @pytest.mark.asyncio
    async def test_a_wrongly_typed_user_value_is_refused(self) -> None:
        service, repo = await _service()

        with pytest.raises(ValueError):
            await service.update_for_user(
                "outbox", uuid.uuid4(), {"undo_send_seconds": "soon"}, is_admin=False,
            )

        assert repo.users == {}

    @pytest.mark.asyncio
    async def test_import_sends_user_keys_to_the_importing_admin(self) -> None:
        service, repo = await _service()
        admin = uuid.uuid4()

        await service.bulk_import(
            {"orders": {"language": "Dutch", "write_model": "w"}, "ai": {"model": "m"}}, admin,
        )

        assert repo.instance["orders"] == {"write_model": "w"}
        assert repo.users[admin]["orders"] == {"language": "Dutch"}
        assert repo.instance["ai"] == {"model": "m"}


@pytest.fixture()
def world() -> Iterator[tuple[FastAPI, SettingsService, _MemoryRepo]]:
    """The settings router over a real service with in-memory storage."""
    from mail_verdict.api.settings_api import router

    service = SettingsService(MagicMock())
    repo = _MemoryRepo()
    service._repo = repo  # type: ignore[assignment]
    import asyncio

    asyncio.run(service.load())
    app = FastAPI()
    app.include_router(router)
    credentials = MagicMock()
    credentials.status = AsyncMock(return_value={"configured": False, "hint": None})
    with (
        patch("mail_verdict.api.settings_api.get_settings_service", return_value=service),
        patch("mail_verdict.api.settings_api.get_provider_credential_repo",
              return_value=credentials),
        patch("mail_verdict.api.settings_api._announce_settings_changed"),
    ):
        yield app, service, repo


def _client_as(app: FastAPI, scope: Any) -> TestClient:
    act_as(app, scope)
    return TestClient(app)


class TestSettingsRoutesForANonAdmin:
    def test_the_overview_holds_only_user_keys(
        self, world: tuple[FastAPI, SettingsService, _MemoryRepo],
    ) -> None:
        app, _, _ = world
        body = _client_as(app, user_scope(uuid.uuid4())).get("/settings").json()

        assert {c: set(keys) for c, keys in body.items()} == USER_KEYS

    def test_an_admin_category_is_refused(
        self, world: tuple[FastAPI, SettingsService, _MemoryRepo],
    ) -> None:
        app, _, _ = world
        client = _client_as(app, user_scope(uuid.uuid4()))

        for category in ("ai", "assistant", "retry", "pipeline", "semantic"):
            assert client.get(f"/settings/{category}").status_code == 403

    def test_a_mixed_category_shows_only_the_users_keys(
        self, world: tuple[FastAPI, SettingsService, _MemoryRepo],
    ) -> None:
        app, _, _ = world
        body = _client_as(app, user_scope(uuid.uuid4())).get("/settings/orders").json()

        assert set(body) == USER_KEYS["orders"]

    def test_writing_an_admin_key_is_a_403_and_stores_nothing(
        self, world: tuple[FastAPI, SettingsService, _MemoryRepo],
    ) -> None:
        app, _, repo = world
        client = _client_as(app, user_scope(uuid.uuid4()))

        refused = [
            client.put("/settings/ai", json={"data": {"model": "x"}}),
            client.put("/settings/orders", json={"data": {"decide_model": "x"}}),
            client.put("/settings/orders", json={"data": {"language": "x", "write_model": "x"}}),
        ]

        assert [r.status_code for r in refused] == [403, 403, 403]
        assert repo.instance == {} and repo.users == {}

    def test_a_provider_key_from_a_non_admin_is_never_stored(
        self, world: tuple[FastAPI, SettingsService, _MemoryRepo],
    ) -> None:
        app, _, repo = world
        credentials = MagicMock()
        with patch(
            "mail_verdict.api.settings_api.get_provider_credential_repo",
            return_value=credentials,
        ):
            response = _client_as(app, user_scope(uuid.uuid4())).put(
                "/settings/mail", json={"data": {"openai_api_key": "sk-secret-value"}},
            )

        assert response.status_code == 403
        credentials.set_key.assert_not_called()

    def test_a_user_write_is_theirs_alone(
        self, world: tuple[FastAPI, SettingsService, _MemoryRepo],
    ) -> None:
        app, _, _ = world
        alice, bob = uuid.uuid4(), uuid.uuid4()

        put = _client_as(app, user_scope(alice)).put(
            "/settings/outbox", json={"data": {"undo_send_seconds": 30.0}},
        )
        alice_view = _client_as(app, user_scope(alice)).get("/settings/outbox").json()
        bob_view = _client_as(app, user_scope(bob)).get("/settings/outbox").json()

        assert put.status_code == 200 and put.json() == {"undo_send_seconds": 30.0}
        assert alice_view == {"undo_send_seconds": 30.0}
        assert bob_view == {"undo_send_seconds": SETTING_DEFAULTS["outbox"]["undo_send_seconds"]}

    def test_a_user_value_is_still_type_checked(
        self, world: tuple[FastAPI, SettingsService, _MemoryRepo],
    ) -> None:
        app, _, _ = world

        response = _client_as(app, user_scope(uuid.uuid4())).put(
            "/settings/outbox", json={"data": {"undo_send_seconds": "later"}},
        )

        assert response.status_code == 400

    def test_orders_validation_runs_against_the_callers_view(
        self, world: tuple[FastAPI, SettingsService, _MemoryRepo],
    ) -> None:
        app, _, _ = world

        response = _client_as(app, user_scope(uuid.uuid4())).put(
            "/settings/orders", json={"data": {"filter": {"include": "not-a-mapping"}}},
        )

        assert response.status_code == 400

    @pytest.mark.parametrize(
        ("method", "path", "body"),
        [
            ("GET", "/settings/providers", None),
            ("POST", "/settings/import", {"data": {}}),
        ],
    )
    def test_admin_routes_are_refused(
        self, world: tuple[FastAPI, SettingsService, _MemoryRepo],
        method: str, path: str, body: Any,
    ) -> None:
        app, _, _ = world

        response = _client_as(app, user_scope(uuid.uuid4())).request(method, path, json=body)

        assert response.status_code == 403
        assert response.json()["detail"] == "admin_only"

    def test_the_schema_is_open_to_everyone(
        self, world: tuple[FastAPI, SettingsService, _MemoryRepo],
    ) -> None:
        app, _, _ = world

        response = _client_as(app, user_scope(uuid.uuid4())).get("/settings/schema")

        assert response.status_code == 200
        assert response.json()["outbox"]["undo_send_seconds"]["scope"] == "user"

    def test_the_account_order_a_user_saves_keeps_only_their_accounts(
        self, world: tuple[FastAPI, SettingsService, _MemoryRepo],
    ) -> None:
        app, _, _ = world
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        client = _client_as(app, user_scope(uuid.uuid4(), account_ids=[mine]))

        put = client.put(
            "/settings/mail", json={"data": {"account_order": [str(theirs), str(mine)]}},
        )

        assert put.json()["account_order"] == [str(mine)]


class TestSettingsRoutesForAnAdmin:
    def test_an_admin_sees_every_key(
        self, world: tuple[FastAPI, SettingsService, _MemoryRepo],
    ) -> None:
        app, _, _ = world
        body = _client_as(app, owner_scope()).get("/settings").json()

        assert set(body) == {c.value for c in SettingCategory}
        assert "model" in body["ai"] and "decide_model" in body["orders"]
        assert "language" in body["orders"]

    def test_an_admin_write_reaches_the_instance_and_their_own_values(
        self, world: tuple[FastAPI, SettingsService, _MemoryRepo],
    ) -> None:
        app, _, repo = world
        admin = owner_scope()

        response = _client_as(app, admin).put(
            "/settings/orders", json={"data": {"decide_model": "m", "language": "German"}},
        )

        assert response.status_code == 200
        assert repo.instance["orders"] == {"decide_model": "m"}
        assert repo.users[admin.require_user()]["orders"] == {"language": "German"}

    def test_the_admins_own_values_do_not_become_everyones(
        self, world: tuple[FastAPI, SettingsService, _MemoryRepo],
    ) -> None:
        app, _, _ = world
        _client_as(app, owner_scope()).put(
            "/settings/orders", json={"data": {"language": "German"}},
        )

        other = _client_as(app, user_scope(uuid.uuid4())).get("/settings/orders").json()

        assert other["language"] == "English"
