#!/usr/bin/env python3
"""Pull every container image the test suite uses, before the suite starts.

Testcontainers pulls on demand, which makes a slow or transient registry
failure surface as an image-inspect 404 in the middle of a test run — the
message names a missing image rather than a failed pull, so it reads like a
configuration error. Pulling up front turns that into an obvious, retried step.

Image tags come from ``tests/setup/images.py`` so this cannot drift from what
the suite actually starts.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.setup import images  # noqa: E402

ATTEMPTS = 3
BACKOFF_SECONDS = 5


def container_cli() -> str:
    """The container tool present here.

    CI runs Docker-in-Docker, which is what testcontainers drives there.
    A developer machine may have only podman, and hardcoding either one
    makes this script fail on the other for no reason -- it only ever
    pulls images, which both do identically.
    """
    for candidate in ("docker", "podman"):
        if shutil.which(candidate):
            return candidate
    raise SystemExit("Neither docker nor podman is on PATH; cannot pull images.")


def image_tags() -> list[str]:
    """Every image constant declared in tests/setup/images.py."""
    return sorted(
        value
        for name, value in vars(images).items()
        if name.endswith("_IMAGE") and isinstance(value, str)
    )


def pull(tag: str) -> bool:
    """Pull one image, retrying a transient registry failure."""
    for attempt in range(1, ATTEMPTS + 1):
        result = subprocess.run(
            [container_cli(), "pull", "--quiet", tag],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"  {tag}")
            return True
        print(
            f"  {tag}: attempt {attempt}/{ATTEMPTS} failed: "
            f"{result.stderr.strip() or result.stdout.strip()}",
            file=sys.stderr,
        )
        if attempt < ATTEMPTS:
            time.sleep(BACKOFF_SECONDS * attempt)
    return False


def main() -> int:
    """Pull every test image, reporting which ones could not be fetched."""
    tags = image_tags()
    if not tags:
        print("No image constants found in tests/setup/images.py", file=sys.stderr)
        return 1

    failed = [tag for tag in tags if not pull(tag)]
    if failed:
        print(f"\nCould not pull: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"\nPulled {len(tags)} images.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
