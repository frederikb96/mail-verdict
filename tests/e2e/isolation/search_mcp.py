"""Text and semantic search, unified views, and every assistant (MCP) tool:
they see the caller's own accounts and views, and nothing of anyone else's."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import patch

from starlette.testclient import TestClient

from mail_verdict.database.connection import get_db_connection
from mail_verdict.database.models import EMBEDDING_DIMENSIONS, MessageEmbedding
from tests.e2e.isolation import RouteCase, ToolCase, World
from tests.helpers.mcp_http import McpHttpSession
from tests.setup.auth import bearer


def _subject(world: World) -> str:
    """The subject of B's message."""
    return next(content for content in world.b_content if "@" not in content)


def _word(world: World) -> str:
    """A word of B's subject: it finds B's message, and echoed back in a
    response it is not one of B's markers."""
    return _subject(world).split()[0]


def _view_name(world: World) -> str:
    """The name of B's unified view."""
    return next(marker for marker in world.b_markers if marker.startswith("B view "))


def _account(world: World) -> dict[str, str]:
    return {"account_id": world.b_account_id}


def _view(world: World) -> dict[str, str]:
    return {"view_id": world.b["view_id"]}


def _mail(world: World) -> dict[str, Any]:
    return {"mail_id": world.b["message_id"]}


def _on_b_account(world: World) -> dict[str, Any]:
    return {
        "account_id": world.b_account_id, "to": ["planted@example.com"],
        "subject": "Planted by A", "body_text": "planted",
    }


ROUTE_CASES: list[RouteCase] = [
    # api/search.py
    RouteCase("GET /api/search", "own", query=lambda world: {"q": _word(world)}),
    # api/embeddings.py -- the stack has no embedding provider, so the query
    # cannot be embedded; the check below searches with a stub for one.
    RouteCase(
        "GET /api/embeddings/search", "own", query=lambda world: {"q": _word(world)},
        also=(503,),
    ),
    # api/unified.py
    RouteCase(
        "PUT /api/accounts/{account_id}/emoji", "account_path", params=_account,
        body=lambda world: {"emoji": "x"},
    ),
    RouteCase("GET /api/unified/folders", "own"),
    RouteCase("GET /api/unified/folder-order", "own"),
    RouteCase("PUT /api/unified/folder-order", "own", body=lambda world: {"order": []}),
    RouteCase(
        "POST /api/unified/views", "own",
        body=lambda world: {"name": f"A view {uuid.uuid4().hex[:8]}"},
    ),
    RouteCase(
        "GET /api/unified/mails", "resource_id",
        query=lambda world: {"folder_name": _view_name(world)},
    ),
    RouteCase(
        "PATCH /api/unified/views/{view_id}", "resource_id", params=_view,
        body=lambda world: {"name": "Taken by A"},
    ),
    RouteCase("DELETE /api/unified/views/{view_id}", "resource_id", params=_view),
]

TOOL_CASES: list[ToolCase] = [
    ToolCase("list_accounts", "own"),
    ToolCase("get_stats", "own"),
    ToolCase("get_semantic_status", "own"),
    ToolCase("search_mail", "own", arguments=lambda world: {"query": _word(world)}),
    ToolCase(
        "semantic_search_mail", "resource_id",
        arguments=lambda world: {"query": _word(world), "account_id": world.b_account_id},
    ),
    ToolCase("get_mail", "resource_id", arguments=_mail),
    ToolCase("get_thread", "resource_id", arguments=_mail),
    ToolCase("get_verdict", "resource_id", arguments=_mail),
    ToolCase(
        "list_mails", "resource_id", arguments=lambda world: {"account_id": world.b_account_id},
    ),
    ToolCase(
        "list_folders", "resource_id", arguments=lambda world: {"account_id": world.b_account_id},
    ),
    ToolCase(
        "mark_mail", "resource_id", arguments=lambda world: {**_mail(world), "is_seen": True},
    ),
    ToolCase(
        "tag_mail", "resource_id",
        arguments=lambda world: {**_mail(world), "tag_name": "planted"},
    ),
    ToolCase(
        "move_mail", "resource_id",
        arguments=lambda world: {**_mail(world), "target_folder": "INBOX"},
    ),
    ToolCase(
        "submit_spam_feedback", "resource_id",
        arguments=lambda world: {**_mail(world), "account_id": world.b_account_id, "is_spam": True},
    ),
    ToolCase("send_mail", "resource_id", arguments=_on_b_account),
    ToolCase("draft_mail", "resource_id", arguments=_on_b_account),
    ToolCase(
        "reply_mail", "resource_id",
        arguments=lambda world: {**_mail(world), "mode": "reply", "body_text": "planted"},
    ),
]


async def _embed_b_message(world: World, model: str) -> None:
    """A vector for B's message under `model`, in the app's own database."""
    async with get_db_connection().session() as session:
        await session.execute(
            MessageEmbedding.__table__.insert().values(
                account_id=uuid.UUID(world.b_account_id), msg_key=f"isolation-{model}",
                message_id=uuid.UUID(world.b["message_id"]), model=model, status="done",
                embedding=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
        )


def semantic_search_finds_only_the_callers_mail(client: TestClient, world: World) -> None:
    """B's message is found by B's meaning search -- over REST and as a tool
    -- and never by A's, whatever account A names."""
    model = f"isolation-{uuid.uuid4().hex[:8]}"
    client.portal.call(_embed_b_message, world, model)

    async def embedded(text: str) -> tuple[str, list[float]]:
        return model, [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)

    mine, theirs = bearer(world.a_token), bearer(world.b_token)
    with patch("mail_verdict.embeddings.service.embed_search_query", embedded):
        for headers, sees in ((theirs, True), (mine, False)):
            for params in ({}, {"account_id": world.b_account_id}):
                response = client.get(
                    "/api/embeddings/search", headers=headers, params={"q": "anything", **params},
                )
                assert response.status_code == 200, response.text
                assert (world.b["message_id"] in response.text) is sees, response.text

        for headers, sees in ((theirs, True), (mine, False)):
            session = McpHttpSession(client, "/mcp/", headers)
            found = session.call_tool(
                "semantic_search_mail", {"query": "anything"}, headers,
            )
            assert (world.b["message_id"] in json.dumps(found)) is sees, found
            named = session.call_tool(
                "semantic_search_mail",
                {"query": "anything", "account_id": world.b_account_id}, headers,
            )
            assert (world.b["message_id"] in json.dumps(named)) is sees, named


def text_search_finds_only_the_callers_mail(client: TestClient, world: World) -> None:
    """B finds their own message by its subject, A finds nothing -- naming
    B's account, folder or message as the cursor changes nothing."""
    subject = _subject(world)
    mine, theirs = bearer(world.a_token), bearer(world.b_token)

    found = client.get("/api/search", headers=theirs, params={"q": subject})
    assert [r["id"] for r in found.json()["results"]] == [world.b["message_id"]]

    for params in (
        {"q": subject},
        {"q": subject, "account_id": world.b_account_id},
        {"q": subject, "folder_ids": world.b["folder_id"]},
    ):
        response = client.get("/api/search", headers=mine, params=params)
        assert response.status_code == 200, response.text
        assert response.json()["results"] == [], params
    cursor = client.get(
        "/api/search", headers=mine, params={"q": subject, "before": world.b["message_id"]},
    )
    assert cursor.status_code == 400, cursor.text

    session = McpHttpSession(client, "/mcp/", theirs)
    assert world.b["message_id"] in json.dumps(
        session.call_tool("search_mail", {"query": subject}, theirs)
    )


def b_view_stays_with_b(client: TestClient, world: World) -> None:
    """B's view lists B's mail for B, and is neither visible nor writable for
    A -- including A putting their own folders into it or B's into theirs."""
    mine, theirs = bearer(world.a_token), bearer(world.b_token)
    name = _view_name(world)

    listed = client.get("/api/unified/mails", headers=theirs, params={"folder_name": name})
    assert listed.status_code == 200, listed.text
    assert world.b["message_id"] in {m["id"] for m in listed.json()["messages"]}, (
        listed.text, client.get("/api/unified/folders", headers=theirs).text,
    )
    assert name not in client.get("/api/unified/folders", headers=mine).text

    # A cannot put B's folder into A's own view, or into B's.
    created = client.post("/api/unified/views", headers=mine, json={"name": f"Mine {name}"})
    assert created.status_code == 201, created.text
    for view_id in (created.json()["id"], world.b["view_id"]):
        response = client.patch(
            f"/api/folders/{world.b['folder_id']}/prefs", headers=mine,
            json={"unified_view_ids": [view_id]},
        )
        assert response.status_code in (403, 404), response.text


CHECKS = [
    semantic_search_finds_only_the_callers_mail,
    text_search_finds_only_the_callers_mail,
    b_view_stays_with_b,
]
