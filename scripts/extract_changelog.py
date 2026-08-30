#!/usr/bin/env python3
"""Print the CHANGELOG section for one version, for use as a release body.

Reads CHANGELOG.md and writes the body of the requested version's section to stdout,
without the heading itself. Used by the release workflow::

    python scripts/extract_changelog.py 1.0.0

Exits non-zero if the version has no section, so a release cannot quietly publish with
an empty body.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

# Matches "## [1.0.0] - 2026-08-29", "## [1.0.0]", "## 1.0.0 - ..." and similar.
HEADING = re.compile(r"^##\s+\[?(?P<version>[^\]\s]+)\]?")


def extract(text: str, version: str) -> str | None:
    """Return the body of the section for `version`, or None if absent."""
    lines = text.splitlines()
    start: int | None = None
    end = len(lines)

    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if not match:
            continue
        if start is None and match.group("version") == version:
            start = index + 1
        elif start is not None:
            end = index
            break

    if start is None:
        return None
    return "\n".join(lines[start:end]).strip()


def main() -> int:
    """Write the requested version's changelog body to stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="version without a leading v, e.g. 1.0.0")
    args = parser.parse_args()

    version = args.version[1:] if args.version.startswith("v") else args.version
    body = extract(CHANGELOG.read_text(encoding="utf-8"), version)

    if body is None:
        print(
            f"CHANGELOG.md has no section for version {version}. "
            "Add one before releasing.",
            file=sys.stderr,
        )
        return 1
    if not body:
        print(f"CHANGELOG.md section for {version} is empty.", file=sys.stderr)
        return 1

    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
