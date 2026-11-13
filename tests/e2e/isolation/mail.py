"""Mail accounts, folders, messages, verdicts, runs, outbox, identities,
image exceptions and notifications -- everything a mail account owns."""

from __future__ import annotations

import uuid
from typing import Any

from starlette.testclient import TestClient

from tests.e2e.helpers import unique_email
from tests.e2e.isolation import RouteCase, World
from tests.setup.auth import bearer
from tests.setup.containers import DOVECOT_ALIAS, DOVECOT_IMAP_PORT, DOVECOT_PASSWORD


def _account(world: World) -> dict[str, str]:
    return {"account_id": world.b_account_id}


def _message(world: World) -> dict[str, str]:
    return {"message_id": world.b["message_id"]}


def _mail(world: World) -> dict[str, str]:
    return {"mail_id": world.b["message_id"]}


def _folder(world: World) -> dict[str, str]:
    return {"folder_id": world.b["folder_id"]}


def _identity(world: World) -> dict[str, str]:
    return {"identity_id": world.b["identity_id"]}


def _run(world: World) -> dict[str, str]:
    return {"run_id": world.b["run_id"]}


def _own_account(world: World) -> dict[str, Any]:
    email = unique_email("iso-own")
    return {
        "name": email, "imap_host": DOVECOT_ALIAS, "imap_port": DOVECOT_IMAP_PORT,
        "imap_user": email, "imap_password": DOVECOT_PASSWORD,
    }


def _draft_on_b(world: World) -> dict[str, Any]:
    return {
        "account_id": world.b_account_id, "kind": "draft", "subject": "Planted by A",
        "body_text": "planted",
    }


ROUTE_CASES: list[RouteCase] = [
    # api/accounts.py
    RouteCase("GET /api/accounts", "own"),
    RouteCase("POST /api/accounts", "own", body=_own_account),
    RouteCase("GET /api/accounts/{account_id}", "account_path", params=_account),
    RouteCase(
        "PATCH /api/accounts/{account_id}", "account_path",
        params=_account, body=lambda world: {"emoji": "x"},
    ),
    RouteCase("DELETE /api/accounts/{account_id}", "account_path", params=_account),
    RouteCase("GET /api/accounts/{account_id}/folders", "account_path", params=_account),
    RouteCase("POST /api/accounts/{account_id}/sync", "account_path", params=_account),
    RouteCase("GET /api/accounts/{account_id}/sync-status", "account_path", params=_account),
    # api/folder_management.py
    RouteCase("GET /api/accounts/{account_id}/folder-order", "account_path", params=_account),
    RouteCase(
        "PUT /api/accounts/{account_id}/folder-order", "account_path",
        params=_account, body=lambda world: {"order": [world.b["folder_id"]]},
    ),
    RouteCase(
        "POST /api/accounts/{account_id}/folders", "account_path",
        params=_account, body=lambda world: {"name": "Planted by A"},
    ),
    RouteCase("DELETE /api/folders/{folder_id}", "resource_id", params=_folder),
    RouteCase(
        "PATCH /api/folders/{folder_id}/prefs", "resource_id",
        params=_folder, body=lambda world: {"is_visible": False},
    ),
    # api/identities.py
    RouteCase("GET /api/identities", "own"),
    RouteCase(
        "POST /api/identities", "resource_id",
        body=lambda world: {"account_id": world.b_account_id, "address": "planted@example.com"},
    ),
    RouteCase(
        "PATCH /api/identities/{identity_id}", "resource_id",
        params=_identity, body=lambda world: {"display_name": "Planted"},
    ),
    RouteCase("DELETE /api/identities/{identity_id}", "resource_id", params=_identity),
    # api/image_exceptions.py
    RouteCase("GET /api/accounts/{account_id}/image-exceptions", "account_path", params=_account),
    RouteCase(
        "POST /api/accounts/{account_id}/image-exceptions", "account_path",
        params=_account, body=lambda world: {"type": "sender", "value": "planted@example.com"},
    ),
    RouteCase(
        "GET /api/accounts/{account_id}/image-exceptions/check", "account_path",
        params=_account, query=lambda world: {"sender": "planted@example.com"},
    ),
    RouteCase(
        "DELETE /api/accounts/{account_id}/image-exceptions/{exception_id}", "account_path",
        params=lambda world: {**_account(world), "exception_id": world.b["exception_id"]},
    ),
    # api/mails.py
    RouteCase("GET /api/accounts/{account_id}/messages", "account_path", params=_account),
    RouteCase(
        "POST /api/accounts/{account_id}/messages/bulk-action", "account_path",
        params=_account,
        body=lambda world: {"action": "mark_read", "ids": [world.b["message_id"]]},
    ),
    RouteCase(
        "GET /api/accounts/{account_id}/messages/selection", "account_path",
        params=_account, query=lambda world: {"folder_id": world.b["folder_id"]},
    ),
    RouteCase("GET /api/messages/{message_id}", "resource_id", params=_message),
    RouteCase(
        "POST /api/messages/{message_id}/action", "resource_id",
        params=_message, body=lambda world: {"action": "mark_read"},
    ),
    RouteCase(
        "GET /api/messages/{message_id}/attachments/{attachment_id}", "resource_id",
        params=lambda world: {**_message(world), "attachment_id": str(uuid.uuid4())},
    ),
    RouteCase("GET /api/messages/{message_id}/location", "resource_id", params=_message),
    RouteCase("GET /api/messages/{message_id}/quote", "resource_id", params=_message),
    RouteCase("GET /api/messages/{message_id}/raw", "resource_id", params=_message),
    RouteCase("GET /api/messages/{message_id}/thread", "resource_id", params=_message),
    # api/notifications.py
    RouteCase("GET /api/accounts/{account_id}/notifications", "account_path", params=_account),
    RouteCase(
        "POST /api/accounts/{account_id}/notifications/ack-all", "account_path",
        params=_account,
    ),
    RouteCase(
        "GET /api/accounts/{account_id}/notifications/unacknowledged-count", "account_path",
        params=_account,
    ),
    RouteCase(
        "POST /api/accounts/{account_id}/notifications/{notification_id}/ack", "account_path",
        params=lambda world: {**_account(world), "notification_id": world.b["notification_id"]},
    ),
    RouteCase(
        "GET /api/notifications", "own",
        query=lambda world: {"unacknowledged_only": "true", "limit": "500"},
    ),
    # api/outbox.py
    RouteCase("GET /api/outbox", "own"),
    RouteCase("POST /api/outbox", "resource_id", body=_draft_on_b),
    RouteCase("GET /api/outbox/pending", "own"),
    RouteCase(
        "GET /api/outbox/pending/{pending_send_id}/attachments/{attachment_id}", "resource_id",
        params=lambda world: {
            "pending_send_id": world.b["pending_send_id"],
            "attachment_id": world.b["pending_attachment_id"],
        },
    ),
    RouteCase(
        "POST /api/outbox/pending/{pending_send_id}/cancel", "resource_id",
        params=lambda world: {"pending_send_id": world.b["pending_send_id"]},
    ),
    # api/runs.py
    RouteCase("GET /api/mails/{mail_id}/runs", "resource_id", params=_mail),
    RouteCase("GET /api/runs", "own"),
    RouteCase("GET /api/runs/{run_id}", "resource_id", params=_run),
    RouteCase("POST /api/runs/{run_id}/retry", "resource_id", params=_run),
    # api/stats.py
    RouteCase("GET /api/stats", "own"),
    # api/verdicts.py
    RouteCase(
        "POST /api/mails/{mail_id}/feedback", "resource_id",
        params=_mail, query=lambda world: {"account_id": world.b_account_id},
        body=lambda world: {"is_spam": False},
    ),
    RouteCase("GET /api/mails/{mail_id}/verdict", "resource_id", params=_mail),
    RouteCase("GET /api/verdicts", "own"),
    RouteCase("GET /api/verdicts/spam-review", "own"),
]


def lists_name_only_the_callers_own_rows(client: TestClient, world: World) -> None:
    """Every list that takes an account (or message) filter keeps A inside A's
    own accounts when the filter names B's, and shows nothing of B's without
    one."""
    mine = bearer(world.a_token)
    b_account = {"account_id": world.b_account_id}
    draft_subject = f"Draft of {world.b_user_id}"
    drafted = client.post(
        "/api/outbox", headers=bearer(world.b_token),
        json={
            "account_id": world.b_account_id, "kind": "draft",
            "subject": draft_subject, "body_text": "B's draft",
        },
    )
    assert drafted.status_code == 201, drafted.text

    accounts = client.get("/api/accounts", headers=mine).json()
    assert world.b_account_id not in {a["id"] for a in accounts}
    assert world.a_account_id in {a["id"] for a in accounts}

    for path, params in (
        ("/api/outbox", b_account),
        ("/api/outbox/pending", b_account),
        ("/api/identities", b_account),
        ("/api/runs", b_account),
        ("/api/verdicts", b_account),
        ("/api/verdicts", {"mail_id": world.b["message_id"]}),
    ):
        response = client.get(path, headers=mine, params=params)
        assert response.status_code == 200, (path, response.text)
        assert response.json() == [], (path, response.text)

    everything = client.get("/api/outbox", headers=mine)
    assert everything.status_code == 200 and draft_subject not in everything.text
    owners = client.get("/api/outbox", headers=bearer(world.b_token))
    assert draft_subject in owners.text, "B's own outbox lost its draft"

    stats = client.get("/api/stats", headers=mine, params=b_account).json()
    assert stats["total_accounts"] == 0 and stats["total_messages"] == 0, stats
    own_stats = client.get("/api/stats", headers=mine).json()
    synced = {row["account_id"] for row in own_stats["account_sync"]}
    assert world.a_account_id in synced and world.b_account_id not in synced

    review = client.get("/api/verdicts/spam-review", headers=mine).json()
    assert review["items"] == []
    owners_review = client.get("/api/verdicts/spam-review", headers=bearer(world.b_token))
    assert world.b["message_id"] in {
        item["message_id"] for item in owners_review.json()["items"]
    }, "B's own spam review lost its row"


def a_body_naming_b_acts_on_nothing_of_b(client: TestClient, world: World) -> None:
    """A message id or a folder id of B's inside a request that is A's own
    changes nothing of B's."""
    mine, theirs = bearer(world.a_token), bearer(world.b_token)
    before = client.get(f"/api/messages/{world.b['message_id']}", headers=theirs).json()

    for action in ("mark_read", "flag", "expunge", "trash"):
        bulk = client.post(
            f"/api/accounts/{world.a_account_id}/messages/bulk-action", headers=mine,
            json={"action": action, "ids": [world.b["message_id"]]},
        )
        assert bulk.status_code == 200, bulk.text
        assert bulk.json()["affected_count"] == 0, bulk.text

    scoped = client.post(
        f"/api/accounts/{world.a_account_id}/messages/bulk-action", headers=mine,
        json={
            "action": "mark_read",
            "scope": {
                "folder_id": world.b["folder_id"], "filter": "all",
                "snapshot_at": "2999-01-01T00:00:00+00:00",
            },
        },
    )
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["affected_count"] == 0, scoped.text

    listed = client.get(
        f"/api/accounts/{world.a_account_id}/messages", headers=mine,
        params={"folder_id": world.b["folder_id"]},
    )
    assert listed.status_code == 200 and listed.json()["messages"] == []
    around = client.get(
        f"/api/accounts/{world.a_account_id}/messages", headers=mine,
        params={"around": world.b["message_id"]},
    )
    assert around.status_code == 404, around.text

    after = client.get(f"/api/messages/{world.b['message_id']}", headers=theirs).json()
    assert (after["is_seen"], after["is_flagged"], after["folder_id"]) == (
        before["is_seen"], before["is_flagged"], before["folder_id"],
    )


def an_idempotency_key_is_each_users_own(client: TestClient, world: World) -> None:
    """The same key from two users is two unrelated submissions: the second
    user is not answered with the first's response."""
    key = str(uuid.uuid4())
    body = {"action": "mark_read", "ids": [], "idempotency_key": key}
    first = client.post(
        f"/api/accounts/{world.a_account_id}/messages/bulk-action",
        headers=bearer(world.a_token), json=body,
    )
    assert first.status_code == 200, first.text
    second = client.post(
        f"/api/accounts/{world.b_account_id}/messages/bulk-action",
        headers=bearer(world.b_token), json=body,
    )
    assert second.status_code == 200, second.text


CHECKS = [
    lists_name_only_the_callers_own_rows,
    a_body_naming_b_acts_on_nothing_of_b,
    an_idempotency_key_is_each_users_own,
]
