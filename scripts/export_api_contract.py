#!/usr/bin/env python3
"""
Export the API contract snapshot under docs/api-contract/.

- openapi.json: the OpenAPI document of the /api app, with the package
  version normalised so a release bump alone does not change the file
- sse-events.json: every SSE event name, sorted

Clients mirroring the API by hand (the iOS app, the web client's own types)
check themselves against these files, so every API change shows up as a
reviewed diff of them.

Usage:
    python scripts/export_api_contract.py           # rewrite the snapshot
    python scripts/export_api_contract.py --check   # exit 1 with a diff if stale
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "docs" / "api-contract"
SNAPSHOT_VERSION = "0.0.0"


def _dump(value: Any) -> str:
    """Serialise deterministically: sorted keys, two-space indent, trailing newline."""
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def render_contract() -> dict[str, str]:
    """
    Build every snapshot file's content from the code.

    Returns:
        File name to file content
    """
    from mail_verdict.api.events import SSE_EVENT_TYPES
    from mail_verdict.server import build_api_app

    document = build_api_app().openapi()
    document["info"]["version"] = SNAPSHOT_VERSION
    return {
        "openapi.json": _dump(document),
        "sse-events.json": _dump(sorted(SSE_EVENT_TYPES)),
    }


def stale_files(directory: Path) -> dict[str, str]:
    """
    Compare the snapshot in a directory against the code.

    Args:
        directory: Where the snapshot files live

    Returns:
        File name to unified diff, for every file that is missing or differs
    """
    diffs: dict[str, str] = {}
    for name, expected in render_contract().items():
        path = directory / name
        actual = path.read_text(encoding="utf-8") if path.exists() else ""
        if actual != expected:
            diffs[name] = "".join(difflib.unified_diff(
                actual.splitlines(keepends=True), expected.splitlines(keepends=True),
                fromfile=f"{name} (committed)", tofile=f"{name} (from code)",
            ))
    return diffs


def main(argv: list[str] | None = None) -> int:
    """
    Write or check the snapshot.

    Args:
        argv: Command-line arguments, sys.argv[1:] when None

    Returns:
        Process exit code
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="fail if the snapshot is stale")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="snapshot directory")
    args = parser.parse_args(argv)

    if args.check:
        diffs = stale_files(args.dir)
        for diff in diffs.values():
            sys.stdout.write(diff)
        if diffs:
            print(
                f"\nAPI contract snapshot is stale ({', '.join(sorted(diffs))}). "
                "Regenerate it with: python scripts/export_api_contract.py",
                file=sys.stderr,
            )
            return 1
        return 0

    args.dir.mkdir(parents=True, exist_ok=True)
    for name, content in render_contract().items():
        (args.dir / name).write_text(content, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
