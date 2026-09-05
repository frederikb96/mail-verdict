"""
Which write paths announce themselves, derived rather than listed.

A write against a table PostIMAP mirrors is announced by PostIMAP's own
NOTIFY; a write against a table this application owns has nothing
upstream to fire one, so the handler has to push the event itself or no
open browser ever learns of it. Naming the handlers that do is how that
coverage was tracked, and a list of what is covered says nothing about
what was added yesterday.

So the set is derived here instead: every mutating route on every router
the application mounts, the tables this application owns taken from its
own migrations, and the models named after them. A route that writes one
of those models has to reach the event ring, or be written down below
with what a second browser sees instead.

What counts as a write is a model constructed, or handed to insert,
update or delete, anywhere the route reaches -- its own helpers and the
methods it calls on a repository it builds. Reading a table, or naming it
in a type, is not. That makes this a check on what it can see from the
route rather than a proof that nothing else writes: it catches the write
that was added and announced nothing, which is the failure that has
actually happened, and it does not certify the ones it cannot follow.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Any

from fastapi.routing import APIRoute

from mail_verdict.api.routes import all_routers
from mail_verdict.database import models

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# A push, never a mention. Naming the ring is what a route does before
# it decides whether to announce, so matching the name alone passes a
# route that fetches the ring and then pushes on only one of its
# branches -- the exact gap this file exists to find.
_ANNOUNCEMENT_MARKERS = (r"event_ring\.add\(", r"_event_ring\.add\(", r"broadcast_event\(")
_MIGRATIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
# Deep enough to reach a handler's helpers and the repository objects it
# builds, without walking the whole application from every route.
_MAX_CALL_DEPTH = 4

# Every route the check finds writing a table this application owns while
# pushing no event of its own, with what that means for a second open
# browser. A route that starts announcing, or stops writing an owned
# table, falls out of the check and fails as a stale entry -- so this
# cannot quietly describe something that is no longer true, and a new
# unannounced write fails until it is either announced or written down.
_UNANNOUNCED: dict[str, str] = {
    "POST /accounts": (
        "The account row is PostIMAP's own and its notification is what a second browser "
        "reacts to; the preferences row is written in the same request and read with it."
    ),
    "POST /accounts/{account_id}/image-exceptions": (
        "A second open browser keeps its own answer for who may load remote images until it "
        "refetches the allowlist, which it does per sender as a message is opened."
    ),
    "DELETE /accounts/{account_id}/image-exceptions/{exception_id}": (
        "The same, in the other direction."
    ),
    "POST /calendar/events/{object_id}/respond": (
        "The RSVP itself is written to the DAV object PostIMAP announces; the reply row "
        "beside it records which outbox row carried the message, and is read with the event."
    ),
    "POST /embeddings/backfill": (
        "No browser surface calls this or watches it -- it is driven by an operator, and "
        "what it produces is read by search."
    ),
}


def _owned_table_names() -> set[str]:
    """The tables this application creates for itself -- everything else
    in the schema belongs to PostIMAP and is announced by it."""
    names: set[str] = set()
    for path in _MIGRATIONS.glob("*.py"):
        names |= set(re.findall(r'op\.create_table\(\s*"([^"]+)"', path.read_text()))
    return names


def _owned_model_names() -> set[str]:
    owned = _owned_table_names()
    return {
        cls.__name__
        for cls in models.Base.__subclasses__()
        if getattr(cls, "__tablename__", None) in owned
    }


def _referenced_names(source: str) -> set[str]:
    """Every name a function calls or mentions -- both a helper it calls
    and a model class it merely names in a query. A generated method whose
    recorded source is not parseable on its own contributes nothing rather
    than stopping the walk."""
    try:
        tree = ast.parse(textwrap.dedent(source))
    except SyntaxError:
        return set()
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }


def _reachable_sources(endpoint: Any) -> list[str]:
    """The handler's own source plus that of everything it reaches inside
    this application: its module's helpers, and the methods of any class
    it constructs (a repository, in practice)."""
    sources: list[str] = []
    seen: set[Any] = {endpoint}
    frontier = [endpoint]
    for _ in range(_MAX_CALL_DEPTH):
        next_frontier: list[Any] = []
        for target in frontier:
            try:
                source = inspect.getsource(target)
            except (OSError, TypeError):
                continue
            sources.append(source)
            module: ModuleType | None = __import__(
                target.__module__, fromlist=["__name__"],
            )
            referenced = _referenced_names(source)
            for name in referenced:
                resolved = getattr(module, name, None)
                if not (inspect.isclass(resolved) or inspect.isfunction(resolved)):
                    continue
                if resolved in seen:
                    continue
                if not getattr(resolved, "__module__", "").startswith("mail_verdict"):
                    continue
                if inspect.isclass(resolved):
                    # Only the methods this source actually calls. A
                    # repository built for one read defines writes too,
                    # and taking all of them reads as the caller
                    # performing every write the class can perform.
                    seen.add(resolved)
                    next_frontier.extend(
                        method
                        for name_, method in inspect.getmembers(resolved, inspect.isfunction)
                        if name_ in referenced
                    )
                elif inspect.isfunction(resolved):
                    seen.add(resolved)
                    next_frontier.append(resolved)
        frontier = next_frontier
    return sources


_WRITE_CALLS = {"insert", "update", "delete"}


def _writes_any(sources: list[str], model_names: set[str]) -> bool:
    """Whether a model is written rather than merely named. Naming one is
    what a type annotation and a read query both do; constructing one, or
    handing it to insert/update/delete, is a write. The difference is the
    whole point -- a route that only reads a table has nothing to
    announce."""
    for source in sources:
        try:
            tree = ast.parse(textwrap.dedent(source))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in model_names:
                return True
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if called in _WRITE_CALLS and any(
                isinstance(arg, ast.Name) and arg.id in model_names for arg in node.args
            ):
                return True
    return False


def _mentions(sources: list[str], names: set[str]) -> bool:
    """`names` are regular expressions, so a marker can require a call
    rather than an identifier."""
    pattern = re.compile("|".join(sorted(names)))
    return any(pattern.search(source) for source in sources)


def _mutating_routes() -> list[tuple[str, APIRoute]]:
    routes: list[tuple[str, APIRoute]] = []
    for router in all_routers:
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            methods = sorted(route.methods & _MUTATING_METHODS)
            if methods:
                routes.append((f"{methods[0]} {route.path}", route))
    return routes


class TestEveryOwnedTableWriteAnnouncesItself:
    def test_there_are_routes_and_owned_models_to_check(self) -> None:
        """A derivation that silently produces nothing would pass every
        assertion below without checking anything at all."""
        assert len(_mutating_routes()) > 20
        assert len(_owned_model_names()) > 10

    def test_a_route_writing_an_owned_table_pushes_its_own_event(self) -> None:
        owned = _owned_model_names()
        missing: list[str] = []
        for key, route in _mutating_routes():
            sources = _reachable_sources(route.endpoint)
            if not _writes_any(sources, owned):
                continue
            if _mentions(sources, set(_ANNOUNCEMENT_MARKERS)):
                continue
            if key in _UNANNOUNCED:
                continue
            missing.append(key)
        assert not missing, (
            "these routes write a table this application owns and announce nothing, so a "
            f"second open browser never learns of the change: {missing}"
        )

    def test_no_exemption_has_outlived_its_reason(self) -> None:
        owned = _owned_model_names()
        stale: list[str] = []
        for key, route in _mutating_routes():
            if key not in _UNANNOUNCED:
                continue
            sources = _reachable_sources(route.endpoint)
            writes_owned = _writes_any(sources, owned)
            announces = _mentions(sources, set(_ANNOUNCEMENT_MARKERS))
            if announces or not writes_owned:
                stale.append(key)
        unknown = sorted(set(_UNANNOUNCED) - {key for key, _ in _mutating_routes()})
        assert not stale, f"these exemptions no longer describe the route: {stale}"
        assert not unknown, f"these exemptions name no route at all: {unknown}"
