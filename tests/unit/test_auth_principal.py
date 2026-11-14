"""How a validated token's subject becomes the user a request acts as.

The owner's subjects are ordinary rows: a subject an admin removed from the
owner signs in as nobody, whatever auth.owner_subjects lists. The owner user
itself stays an enabled admin whatever the table says about it.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from mail_verdict.auth.principal import (
    OWNER_USER_ID,
    PrincipalResolver,
    UnknownUser,
    UserDisabled,
    _DirectoryEntry,
    display_name_from_claims,
)


class _Directory:
    """An in-memory UserDirectory recording what the resolver asked of it."""

    def __init__(self, entries: dict[str, _DirectoryEntry] | None = None) -> None:
        self.entries = dict(entries or {})
        self.provisioned: list[tuple[str, str | None, str]] = []
        self.finds = 0

    async def find(self, subject: str) -> _DirectoryEntry | None:
        self.finds += 1
        return self.entries.get(subject)

    async def provision(
        self, subject: str, email: str | None, display_name: str,
    ) -> _DirectoryEntry:
        self.provisioned.append((subject, email, display_name))
        entry = _DirectoryEntry(user_id=uuid.uuid4(), role="user", disabled=False)
        self.entries[subject] = entry
        return entry

    async def touch(self, user_id: uuid.UUID, email: str | None) -> None:
        pass


def _resolver(directory: _Directory, *, auto_provision: bool = True) -> PrincipalResolver:
    return PrincipalResolver(directory, auto_provision=auto_provision, cache_seconds=30)


def _claims(sub: str, **extra: Any) -> dict[str, Any]:
    return {"sub": sub, **extra}


@pytest.mark.asyncio
async def test_an_owner_subject_is_the_owner_and_admin_through_the_table() -> None:
    directory = _Directory({
        "owner-login": _DirectoryEntry(user_id=OWNER_USER_ID, role="user", disabled=False),
    })

    principal = await _resolver(directory).resolve(_claims("owner-login"))

    assert principal.user_id == OWNER_USER_ID
    assert principal.is_admin
    assert directory.finds == 1


@pytest.mark.asyncio
async def test_a_subject_removed_from_the_owner_is_nobody() -> None:
    """Configuration no longer outranks the table: an owner subject that is
    not in user_identities is an unknown subject like any other."""
    directory = _Directory()

    with pytest.raises(UnknownUser):
        await _resolver(directory, auto_provision=False).resolve(_claims("owner-agent"))


@pytest.mark.asyncio
async def test_an_owner_subject_disabled_in_the_table_is_still_let_in() -> None:
    directory = _Directory({
        "owner-login": _DirectoryEntry(user_id=OWNER_USER_ID, role="user", disabled=True),
    })

    principal = await _resolver(directory).resolve(_claims("owner-login"))

    assert principal.user_id == OWNER_USER_ID and principal.is_admin


@pytest.mark.asyncio
async def test_a_known_subject_is_its_user_with_its_role() -> None:
    alice, admin = uuid.uuid4(), uuid.uuid4()
    directory = _Directory({
        "alice": _DirectoryEntry(user_id=alice, role="user", disabled=False),
        "root": _DirectoryEntry(user_id=admin, role="admin", disabled=False),
    })
    resolver = _resolver(directory)

    first = await resolver.resolve(_claims("alice"))
    second = await resolver.resolve(_claims("root"))

    assert (first.user_id, first.is_admin) == (alice, False)
    assert (second.user_id, second.is_admin) == (admin, True)


@pytest.mark.asyncio
async def test_a_disabled_user_is_refused() -> None:
    directory = _Directory({
        "alice": _DirectoryEntry(user_id=uuid.uuid4(), role="user", disabled=True),
    })

    with pytest.raises(UserDisabled):
        await _resolver(directory).resolve(_claims("alice"))


@pytest.mark.asyncio
async def test_an_unknown_subject_becomes_an_ordinary_user_when_provisioning_is_on() -> None:
    directory = _Directory()

    principal = await _resolver(directory).resolve(
        _claims("new-sub", email="kim@example.com", name="Kim"),
    )

    assert not principal.is_admin
    assert principal.user_id != OWNER_USER_ID
    assert directory.provisioned == [("new-sub", "kim@example.com", "Kim")]


@pytest.mark.asyncio
async def test_an_unknown_subject_is_refused_when_provisioning_is_off() -> None:
    directory = _Directory()

    with pytest.raises(UnknownUser):
        await _resolver(directory, auto_provision=False).resolve(_claims("new-sub"))
    assert directory.provisioned == []


@pytest.mark.asyncio
async def test_a_result_is_remembered_until_forgotten() -> None:
    alice = uuid.uuid4()
    directory = _Directory({"alice": _DirectoryEntry(user_id=alice, role="user", disabled=False)})
    resolver = _resolver(directory)

    await resolver.resolve(_claims("alice"))
    directory.entries["alice"] = _DirectoryEntry(user_id=alice, role="user", disabled=True)
    await resolver.resolve(_claims("alice"))
    assert directory.finds == 1

    resolver.forget(alice)
    with pytest.raises(UserDisabled):
        await resolver.resolve(_claims("alice"))


def test_a_new_users_name_falls_back_from_name_to_login_to_email_to_subject() -> None:
    assert display_name_from_claims({"sub": "s", "name": "Kim", "email": "k@x"}) == "Kim"
    assert display_name_from_claims({"sub": "s", "preferred_username": "kim"}) == "kim"
    assert display_name_from_claims({"sub": "s", "email": "k@x"}) == "k@x"
    assert display_name_from_claims({"sub": "s"}) == "s"
