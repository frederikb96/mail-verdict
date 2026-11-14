"""Which sign-in a token belongs to, and when a revoked one stays refused.

The phone case is the one that must fail closed: a native app token whose
jti is not in Zitadel's `<session>-at_<token>` shape is refused, because a
phone the server cannot tell apart from the person's other phones is a phone
nobody can revoke.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from mail_verdict.auth.principal import Principal
from mail_verdict.auth.sign_ins import (
    DeviceRevoked,
    DeviceUnrecognised,
    device_name,
    identify,
    zitadel_session,
)
from tests.helpers.sign_ins import InMemorySignInStore, registry

ALICE = uuid.uuid4()


def _now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _phone(session: str = "V2_3001", token: str = "at_9001", **extra: Any) -> dict[str, Any]:
    return {"sub": "alice", "azp": "native-app", "jti": f"{session}-{token}", "iat": _now(),
            **extra}


def _browser(auth_time: int | None = None, **extra: Any) -> dict[str, Any]:
    return {"sub": "alice", "azp": "web", "sid": "sso-1",
            "auth_time": _now() if auth_time is None else auth_time, "iat": _now(), **extra}


def _machine(sub: str = "agent-key") -> dict[str, Any]:
    return {"sub": sub, "azp": "agent-key", "jti": "V2_77-at_1", "iat": _now()}


def _alice(subject: str = "alice") -> Principal:
    return Principal(user_id=ALICE, subject=subject, is_admin=False)


def _identify(claims: dict[str, Any]) -> tuple[str, str]:
    presented = identify(claims, ALICE, native_client_id="native-app", native_handle="zitadel_jti")
    return presented.kind, presented.handle


def test_a_phone_is_its_zitadel_session_the_same_across_refreshes() -> None:
    assert _identify(_phone(token="at_1")) == ("phone", "V2_3001")
    assert _identify(_phone(token="at_2")) == ("phone", "V2_3001")
    assert _identify(_phone(session="V2_3002")) == ("phone", "V2_3002")


def test_the_native_client_is_recognised_by_client_id_as_well() -> None:
    claims = {"sub": "alice", "client_id": "native-app", "jti": "V2_5-at_6", "iat": _now()}

    assert _identify(claims) == ("phone", "V2_5")


@pytest.mark.parametrize(
    "jti", [None, 42, "", "plain-id", "V2_1-rt_2", "V2_1-at_", "-at_2", "V2_1-at_2-at_3", "V2_1"],
)
def test_a_native_token_without_a_session_handle_is_refused(jti: Any) -> None:
    claims = _phone()
    claims["jti"] = jti

    with pytest.raises(DeviceUnrecognised):
        _identify(claims)


def test_the_subject_strategy_lists_one_phone_per_person() -> None:
    presented = identify(
        {"sub": "alice", "azp": "native-app", "iat": _now()}, ALICE,
        native_client_id="native-app", native_handle="subject",
    )

    assert (presented.kind, presented.handle) == ("phone", "alice")


def test_an_id_token_is_the_persons_browsers_and_a_machine_token_its_subject() -> None:
    assert _identify(_browser()) == ("browser", str(ALICE))
    claims = {"sub": "alice", "auth_time": _now(), "iat": _now()}
    assert _identify(claims) == ("browser", str(ALICE))
    assert _identify(_machine()) == ("machine", "agent-key")


def test_without_a_native_client_every_access_token_is_a_machine() -> None:
    presented = identify(_phone(), ALICE, native_client_id="", native_handle="zitadel_jti")

    assert presented.kind == "machine"


def test_zitadel_session_reads_only_the_documented_shape() -> None:
    assert zitadel_session("V2_1-at_2") == "V2_1"
    assert zitadel_session("V2_1-rt_2") is None


def test_a_device_name_is_utf8_without_control_characters_and_at_most_64_long() -> None:
    assert device_name("Kim’s iPhone".encode()) == "Kim’s iPhone"
    assert device_name(b"  Pixel\x00 8\n ") == "Pixel 8"
    assert device_name(("x" * 80).encode()) == "x" * 64
    assert device_name(b"") is None
    assert device_name(None) is None
    assert device_name(b"\x01\x02") is None


@pytest.mark.asyncio
async def test_a_new_sign_in_is_recorded_with_its_name_and_admitted() -> None:
    store = InMemorySignInStore()

    principal = await registry(store).admit(_alice(), _phone(), "Pixel 8")

    row = store.rows[("phone", "V2_3001")]
    assert principal.sign_in_id == row.id
    assert (row.user_id, row.name, row.subject) == (ALICE, "Pixel 8", "alice")


@pytest.mark.asyncio
async def test_a_revoked_phone_is_refused_after_any_refresh() -> None:
    store = InMemorySignInStore()
    gate = registry(store)
    await gate.admit(_alice(), _phone(token="at_1"), "Pixel 8")
    store.revoke("phone", "V2_3001")

    with pytest.raises(DeviceRevoked):
        await gate.admit(_alice(), _phone(token="at_2", iat=_now() + 3600), "Pixel 8")


@pytest.mark.asyncio
async def test_signing_in_again_on_the_same_phone_is_a_new_sign_in() -> None:
    store = InMemorySignInStore()
    gate = registry(store)
    await gate.admit(_alice(), _phone(session="V2_1"), "Pixel 8")
    store.revoke("phone", "V2_1")

    principal = await gate.admit(_alice(), _phone(session="V2_2"), "Pixel 8")

    assert principal.sign_in_id == store.rows[("phone", "V2_2")].id


@pytest.mark.asyncio
async def test_a_revoked_machine_stays_refused_with_freshly_minted_tokens() -> None:
    store = InMemorySignInStore()
    gate = registry(store)
    await gate.admit(_alice("agent-key"), _machine(), None)
    store.revoke("machine", "agent-key")

    later = {**_machine(), "jti": "V2_78-at_1", "iat": _now() + 3600}
    with pytest.raises(DeviceRevoked):
        await gate.admit(_alice("agent-key"), later, None)


@pytest.mark.asyncio
async def test_a_revoked_browser_needs_a_fresh_authentication_not_a_refreshed_token() -> None:
    store = InMemorySignInStore()
    gate = registry(store)
    signed_in = _now() - 600
    await gate.admit(_alice(), _browser(auth_time=signed_in), None)
    store.revoke("browser", str(ALICE), at=datetime.now(timezone.utc) - timedelta(seconds=60))

    refreshed = _browser(auth_time=signed_in, iat=_now() + 3600)
    with pytest.raises(DeviceRevoked):
        await gate.admit(_alice(), refreshed, None)

    principal = await gate.admit(_alice(), _browser(auth_time=_now()), None)
    assert principal.sign_in_id is not None
    assert store.rows[("browser", str(ALICE))].revoked_at is None


@pytest.mark.asyncio
async def test_a_remembered_result_skips_the_store_until_forgotten() -> None:
    store = InMemorySignInStore()
    gate = registry(store, cache_seconds=30)
    await gate.admit(_alice(), _phone(), "Pixel 8")
    await gate.admit(_alice(), _phone(token="at_2"), "Pixel 8")
    assert store.writes == 1

    store.revoke("phone", "V2_3001")
    gate.forget()
    with pytest.raises(DeviceRevoked):
        await gate.admit(_alice(), _phone(), "Pixel 8")


@pytest.mark.asyncio
async def test_a_new_name_is_written_through_whatever_is_remembered() -> None:
    store = InMemorySignInStore()
    gate = registry(store, cache_seconds=30)
    await gate.admit(_alice(), _phone(), "Pixel 8")

    await gate.admit(_alice(), _phone(), "Kim's Pixel")

    assert store.rows[("phone", "V2_3001")].name == "Kim's Pixel"
