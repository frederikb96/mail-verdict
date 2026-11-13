"""The pipeline document routes: every user's rules are their own, and an
admin sees other users' documents as counts only."""

from __future__ import annotations

from typing import Any

from starlette.testclient import TestClient

from tests.e2e.isolation import RouteCase, World
from tests.setup.auth import bearer

_RULE = "isolation-rule"
_EXTRA = "isolation-extra"


def _match(stage_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "stage_id": stage_id, "type": "match", "name": f"Rule {stage_id}",
        "config": {"when": {"subject_contains": "nothing"}, "effects": [{"tag": {"add": ["x"]}}]},
        **extra,
    }


def _b_mail(world: World) -> dict[str, str]:
    return {"message_id": world.b["message_id"]}


# The own cases run in this order against A's document: the PUT leaves one
# rule, the next ones add, change, reorder and remove another.
ROUTE_CASES: list[RouteCase] = [
    RouteCase("GET /api/pipeline", "own"),
    RouteCase("GET /api/pipeline/stage-types", "own"),
    RouteCase(
        "PUT /api/pipeline", "own",
        body=lambda world: {"enabled": True, "stages": [_match(_RULE)]},
    ),
    RouteCase("POST /api/pipeline/stages", "own", body=lambda world: _match(_EXTRA)),
    RouteCase(
        "PATCH /api/pipeline/stages/{stage_id}", "own",
        params=lambda world: {"stage_id": _EXTRA}, body=lambda world: {"enabled": True},
    ),
    RouteCase(
        "POST /api/pipeline/stages/reorder", "own",
        body=lambda world: {"order": [_EXTRA, _RULE]},
    ),
    RouteCase(
        "POST /api/pipeline/stages/{stage_id}/test", "resource_id",
        params=lambda world: {"stage_id": _RULE}, body=_b_mail,
    ),
    RouteCase("POST /api/pipeline/test", "resource_id", body=_b_mail),
    RouteCase(
        "POST /api/pipeline/assistant", "resource_id",
        body=lambda world: {**_b_mail(world), "prompt": "file mails like this"},
    ),
    RouteCase("GET /api/pipeline/health", "own"),
    RouteCase("GET /api/pipeline/revisions", "own"),
    RouteCase(
        "POST /api/pipeline/revisions/{revision}/restore", "resource_id",
        params=lambda world: {"revision": world.b["pipeline_revision"]},
    ),
    RouteCase(
        "DELETE /api/pipeline/stages/{stage_id}", "own",
        params=lambda world: {"stage_id": _EXTRA},
    ),
    RouteCase("GET /api/admin/pipeline/overview", "admin"),
]


def a_rule_of_b_stays_b(client: TestClient, world: World) -> None:
    """B's rules cannot be read, changed or restored by A, A cannot aim a
    rule at B's account or write a webhook, and the admin overview carries
    none of B's rule content."""
    name = f"B private rule {world.b_user_id[:8]}"
    stage_id = f"b-private-{world.b_user_id[:8]}"
    mine, theirs = bearer(world.a_token), bearer(world.b_token)

    written = client.put("/api/pipeline", headers=theirs, json={"enabled": True, "stages": [
        _match(stage_id, name=name, accounts=[world.b_account_id]),
    ]})
    assert written.status_code == 200, written.text
    b_revision = written.json()["revision"]

    seen = client.get("/api/pipeline", headers=mine)
    assert stage_id not in seen.text and name not in seen.text
    history = client.get("/api/pipeline/revisions", headers=mine).json()
    assert b_revision not in [r["revision"] for r in history]
    assert client.patch(
        f"/api/pipeline/stages/{stage_id}", headers=mine, json={"enabled": False},
    ).status_code == 404
    assert client.delete(f"/api/pipeline/stages/{stage_id}", headers=mine).status_code == 404
    assert client.post(
        f"/api/pipeline/revisions/{b_revision}/restore", headers=mine,
    ).status_code == 404

    aimed = client.put("/api/pipeline", headers=mine, json={"enabled": True, "stages": [
        _match("aimed", accounts=[world.b_account_id]),
    ]})
    assert aimed.status_code == 400 and "foreign_account" in aimed.text
    webhook = {"webhook": {"name": "leak", "url": "https://hooks.example/in"}}
    forwarded = client.put("/api/pipeline", headers=mine, json={"enabled": True, "stages": [{
        **_match("forward"), "config": {"when": {"subject_contains": "x"}, "effects": [webhook]},
    }]})
    assert forwarded.status_code == 400 and "effect_not_allowed" in forwarded.text

    still = client.get("/api/pipeline", headers=theirs).json()
    assert [s["stage_id"] for s in still["stages"]] == [stage_id]

    overview = client.get("/api/admin/pipeline/overview", headers=bearer(world.owner_token))
    assert overview.status_code == 200
    row = next(r for r in overview.json() if r["user_id"] == world.b_user_id)
    assert (row["stages"], row["enabled_stages"]) == (1, 1)
    assert name not in overview.text and stage_id not in overview.text


CHECKS = [a_rule_of_b_stays_b]
