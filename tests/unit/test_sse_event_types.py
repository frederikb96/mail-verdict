"""Every SSE event name the source emits is in SSE_EVENT_TYPES, and nothing else is.

The registry is what the exported contract (docs/api-contract/sse-events.json) is
built from, so a name emitted without a registry entry is an event no client
was told about -- and EventRing.add refuses it at runtime, which would surface
as a failing request in production rather than here.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest

from mail_verdict.api.event_ring import EventRing
from mail_verdict.api.events import SSE_EVENT_TYPES

SRC = Path(__file__).resolve().parents[2] / "src" / "mail_verdict"

# Call shapes that name an event type, and which positional argument does.
# `.add` is EventRing.add (account_id, event_type, data); broadcast_event is
# (db, event_ring, event_type, data); _format_sse is (event_id, event_type,
# data) and is how `connected` and `resync` reach a client without the ring.
_METHOD_ARG = {"add": 1}
_FUNCTION_ARG = {"broadcast_event": 2, "_format_sse": 1}


def _literals(node: ast.expr) -> list[str]:
    """String literals an argument can evaluate to (both arms of a conditional)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _literals(node.body) + _literals(node.orelse)
    return []


def _names_assigned_from_ifexp(tree: ast.AST) -> dict[str, list[str]]:
    """`x = "a" if ... else "b"` -- the one dynamic shape the server uses."""
    found: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.IfExp):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found.setdefault(target.id, []).extend(_literals(node.value))
    return found


def _is_ring(node: ast.expr) -> bool:
    """Whether a call receiver is an EventRing by this codebase's naming."""
    name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
    return name.endswith("event_ring") or name == "ring"


def _emitted_names() -> dict[str, list[str]]:
    """Every event name literal reaching an emit call, mapped to where it occurs."""
    emitted: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        ifexp_names = _names_assigned_from_ifexp(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            arg: ast.expr | None = None
            func = node.func
            is_ring_method = (
                isinstance(func, ast.Attribute)
                and func.attr in _METHOD_ARG
                and _is_ring(func.value)
            )
            if isinstance(func, ast.Attribute) and is_ring_method:
                index = _METHOD_ARG[func.attr]
                arg = node.args[index] if len(node.args) > index else None
                arg = next((k.value for k in node.keywords if k.arg == "event_type"), arg)
            elif isinstance(func, ast.Name) and func.id in _FUNCTION_ARG:
                index = _FUNCTION_ARG[func.id]
                arg = node.args[index] if len(node.args) > index else None
            if arg is None:
                continue
            names = _literals(arg)
            if isinstance(arg, ast.Name):
                names = ifexp_names.get(arg.id, [])
            for name in names:
                emitted.setdefault(name, []).append(f"{path.relative_to(SRC)}:{node.lineno}")
    return emitted


def test_every_emitted_event_name_is_registered() -> None:
    emitted = _emitted_names()
    # A scan that finds nothing would pass vacuously; the server alone emits
    # well over a dozen names.
    assert len(emitted) > 10
    unregistered = {name: sites for name, sites in emitted.items() if name not in SSE_EVENT_TYPES}
    assert not unregistered, f"Emitted but missing from SSE_EVENT_TYPES: {unregistered}"


def test_every_registered_event_name_is_emitted_somewhere() -> None:
    """A stale entry advertises an event to clients that nothing ever sends."""
    never_emitted = SSE_EVENT_TYPES - set(_emitted_names())
    assert not never_emitted, f"In SSE_EVENT_TYPES but emitted nowhere: {sorted(never_emitted)}"


@pytest.mark.asyncio
async def test_ring_refuses_an_unregistered_event_type() -> None:
    ring = EventRing()
    with pytest.raises(ValueError, match="mail.arrived"):
        await ring.add(uuid.uuid4(), "mail.arrived", {})
    assert ring.get_latest_seq() == 0
