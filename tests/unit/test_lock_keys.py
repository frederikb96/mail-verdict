"""
Every PostgreSQL advisory-lock key used anywhere in the package must be
distinct. Two reconciliation timers -- or a timer and a state-write lock,
as `queue/manager.py`'s reclaim timer and `embeddings/worker.py`'s
backfill reconciler once were -- sharing a key serialize behind each
other even though nothing about their purpose overlaps. The failure is a
stall, not an error: `set_state` (an API-level pause/resume) blocking for
the duration of an unrelated embedding backfill batch looks exactly like
a slow request, and nothing anywhere names the shared key as the cause.

The set of `*_LOCK_KEY` constants is derived by parsing the package's own
source rather than hand-maintained here -- a hand-written list stops
being true the moment the next lock is added, which is exactly the shape
of bug this guards against in the first place.
"""

from __future__ import annotations

import ast
from pathlib import Path

import mail_verdict

_LOCK_KEY_SUFFIX = "_LOCK_KEY"


def _find_lock_key_constants() -> list[tuple[str, int]]:
    """Every module-level `SOMETHING_LOCK_KEY = <int literal>` assignment
    under the package, found by parsing source rather than importing it.

    Importing every module in the package would run code that expects a
    live database and application settings to already exist, neither of
    which a static sweep needs or should depend on.

    Returns:
        (qualified constant name, value) for every constant found
    """
    package_root = Path(mail_verdict.__file__).parent
    found: list[tuple[str, int]] = []
    for path in sorted(package_root.rglob("*.py")):
        module_name = (
            path.relative_to(package_root).with_suffix("").as_posix().replace("/", ".")
        )
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Constant) or not isinstance(
                node.value.value, int,
            ):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.endswith(_LOCK_KEY_SUFFIX):
                    found.append((f"{module_name}.{target.id}", node.value.value))
    return found


def test_every_lock_key_constant_is_unique() -> None:
    """A collision serializes two unrelated locks behind each other,
    silently -- the caller sees a stall, never an error naming either
    constant."""
    constants = _find_lock_key_constants()
    assert constants, "sweep found no *_LOCK_KEY constants -- the sweep itself is broken"

    by_value: dict[int, list[str]] = {}
    for name, value in constants:
        by_value.setdefault(value, []).append(name)

    collisions = {value: names for value, names in by_value.items() if len(names) > 1}
    assert not collisions, f"advisory lock key collision(s): {collisions}"
