"""The access check every read and write goes through, and account naming.

An empty scope must filter to nothing -- the one way a scope could leak is by
turning "no accounts" into "no filter".
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from mail_verdict.auth.ownership import account_display_name, owned_account_name
from mail_verdict.auth.scope import ADMIN_RING_KEY, EVERYONE_RING_KEY, Scope, ScopeDenied
from mail_verdict.database.models import Message

MINE, THEIRS, DAV = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _scope(*, is_admin: bool = False) -> Scope:
    return Scope(
        user_id=uuid.uuid4(), is_admin=is_admin,
        account_ids=frozenset({MINE}), dav_account_ids=frozenset({DAV}),
    )


def _sql(scope: Scope) -> str:
    statement = select(Message.id).where(scope.account_filter(Message.account_id))
    return str(statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    ))


def test_require_account_passes_an_own_account_and_refuses_anyone_elses() -> None:
    scope = _scope()

    scope.require_account(MINE)
    for other in (THEIRS, None):
        with pytest.raises(ScopeDenied) as raised:
            scope.require_account(other)
        assert raised.value.status_code == 403


def test_an_admin_scope_is_not_every_account() -> None:
    with pytest.raises(ScopeDenied):
        _scope(is_admin=True).require_account(THEIRS)


def test_the_account_filter_names_exactly_the_owned_accounts() -> None:
    sql = _sql(_scope())

    assert str(MINE) in sql
    assert str(THEIRS) not in sql


@pytest.mark.parametrize("scope", [Scope.system(), Scope(user_id=uuid.uuid4(), is_admin=True)])
def test_an_empty_scope_filters_to_nothing_rather_than_to_everything(scope: Scope) -> None:
    sql = _sql(scope)

    # SQLAlchemy renders an empty IN as a predicate that is never true.
    assert "WHERE" in sql
    assert "1 != 1" in sql or "NULL" in sql.upper() or "false" in sql.lower()


def test_narrow_intersects_a_client_named_account_with_the_owned_ones() -> None:
    scope = _scope()

    assert scope.narrow(None) == frozenset({MINE})
    assert scope.narrow(MINE) == frozenset({MINE})
    assert scope.narrow(THEIRS) == frozenset()


def test_system_scope_acts_for_nobody() -> None:
    system = Scope.system()

    assert system.is_system
    with pytest.raises(ScopeDenied):
        system.require_user()
    with pytest.raises(ScopeDenied):
        system.require_admin()


def test_ring_keys_carry_owned_accounts_the_user_and_admin_only_for_admins() -> None:
    user = _scope()
    admin = _scope(is_admin=True)

    assert user.ring_keys() == {str(MINE), str(DAV), str(user.user_id), EVERYONE_RING_KEY}
    assert admin.ring_keys() == {
        str(MINE), str(DAV), str(admin.user_id), ADMIN_RING_KEY, EVERYONE_RING_KEY,
    }


def test_a_stored_account_name_is_shown_without_its_owner_prefix() -> None:
    owner = uuid.uuid4()

    stored = owned_account_name(owner, "Work/Old")

    assert stored == f"{owner}/Work/Old"
    assert account_display_name(stored) == "Work/Old"


@pytest.mark.parametrize("name", ["Posteo", "Work/Old", "not-a-uuid/Inbox"])
def test_a_name_without_an_owner_prefix_is_shown_as_stored(name: str) -> None:
    assert account_display_name(name) == name
