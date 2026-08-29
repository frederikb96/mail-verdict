#!/usr/bin/env python3
"""Verify that every place a version is written agrees with the release tag.

The project has one version source, ``pyproject.toml``. The Helm chart repeats it twice
(``version`` and ``appVersion``) because Helm requires them in the chart metadata, and a
git tag names it a fourth time. Those four can drift, and when they do the symptom is a
published artifact that claims to be a version it is not.

Run without arguments to check the repository is self-consistent. Pass a tag to also
check the tag agrees::

    python scripts/check_release_versions.py
    python scripts/check_release_versions.py v1.0.0

Exits non-zero and prints every disagreement it found, not just the first.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
CHART = REPO_ROOT / "charts" / "mail-verdict" / "Chart.yaml"

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def read_pyproject_version() -> str:
    """Return the version declared in pyproject.toml."""
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    try:
        return str(data["project"]["version"])
    except KeyError as exc:  # pragma: no cover - malformed pyproject is a hard error
        raise SystemExit(f"{PYPROJECT}: no [project] version field") from exc


def read_chart_versions(path: Path) -> tuple[str | None, str | None]:
    """Return (version, appVersion) from a Chart.yaml.

    Parsed with a line matcher rather than a YAML library so the script has no
    dependencies and can run on a bare CI runner before anything is installed.
    """
    version: str | None = None
    app_version: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("version:") and version is None:
            version = stripped.split(":", 1)[1].strip().strip("\"'")
        elif stripped.startswith("appVersion:") and app_version is None:
            app_version = stripped.split(":", 1)[1].strip().strip("\"'")
    return version, app_version


def normalise_tag(tag: str) -> str:
    """Strip a leading 'v' from a release tag."""
    return tag[1:] if tag.startswith("v") else tag


def main() -> int:
    """Compare every version location and report all disagreements."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", nargs="?", help="release tag, e.g. v1.0.0")
    args = parser.parse_args()

    problems: list[str] = []

    source = read_pyproject_version()
    if not SEMVER.match(source):
        problems.append(f"pyproject.toml version {source!r} is not a semantic version")
    print(f"pyproject.toml       {source}")

    if CHART.exists():
        chart_version, chart_app_version = read_chart_versions(CHART)
        print(f"Chart.yaml version   {chart_version}")
        print(f"Chart.yaml appVersion {chart_app_version}")
        if chart_version != source:
            problems.append(
                f"Chart.yaml version {chart_version!r} does not match pyproject {source!r}"
            )
        if chart_app_version != source:
            problems.append(
                f"Chart.yaml appVersion {chart_app_version!r} does not match pyproject {source!r}"
            )
    else:
        problems.append(f"{CHART} is missing — the chart must ship with the release")

    if args.tag:
        tag_version = normalise_tag(args.tag)
        print(f"git tag              {args.tag}")
        if tag_version != source:
            problems.append(
                f"tag {args.tag!r} does not match pyproject version {source!r}"
            )

    if problems:
        print("\nRelease version check FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print("\nRelease version check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
