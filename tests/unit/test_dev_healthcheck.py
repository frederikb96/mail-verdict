"""Unit tests for the development container images' own healthchecks.

--reload pre-binds uvicorn's listening socket once, in the reloader
process, and reuses it across every worker generation -- a worker that
dies at startup leaves that socket open and accepting connections, so a
probe with no timeout of its own can hang on a response that will never
come rather than failing outright. These parse compose.dev.yaml and
docker/Dockerfile's dev-stage HEALTHCHECK directly, rather than
duplicating their values here, so a change to either is what these
assert against.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# A crashed reload worker being reported healthy for longer than this is
# the regression under test -- unrelated to how fast any one probe fails,
# since the status only flips once `retries` consecutive probes have.
_MAX_DETECTION_WINDOW_S = 60.0


def _seconds(duration: str) -> float:
    """Parse a compose/Dockerfile-style duration ('5s', '45s') to seconds --
    the only unit either file under test actually uses."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)s", duration)
    assert match, f"unexpected duration format: {duration!r}"
    return float(match.group(1))


def test_compose_dev_healthcheck_bounds_curl_itself() -> None:
    """A curl with no --max-time can hang indefinitely against the
    reloader's dangling socket (see compose.dev.yaml's own comment) --
    the outer `timeout:` only kills it if the container engine actually
    enforces it, which a request that never even attempts to return
    does not exercise. --max-time must be strictly under the
    healthcheck's own timeout, so curl itself reports failure first."""
    compose = yaml.safe_load((REPO_ROOT / "compose.dev.yaml").read_text())
    healthcheck = compose["services"]["app"]["healthcheck"]
    test_cmd = healthcheck["test"]
    assert "--max-time" in test_cmd, "curl must bound its own request"
    max_time = float(test_cmd[test_cmd.index("--max-time") + 1])
    assert 0 < max_time < _seconds(healthcheck["timeout"])


def test_compose_dev_healthcheck_detects_a_dead_worker_quickly() -> None:
    """Status only flips from healthy once `retries` consecutive probes
    fail -- a crashed reload worker can otherwise sit reported healthy
    for minutes even though every individual probe against it fails."""
    compose = yaml.safe_load((REPO_ROOT / "compose.dev.yaml").read_text())
    healthcheck = compose["services"]["app"]["healthcheck"]
    window = _seconds(healthcheck["interval"]) * healthcheck["retries"]
    assert window <= _MAX_DETECTION_WINDOW_S, (
        f"a dead dev worker takes {window}s to be reported unhealthy"
    )


def test_dockerfile_dev_healthcheck_matches_compose() -> None:
    """The image's own baked-in HEALTHCHECK is what a compose-less `podman
    run` of the dev image relies on -- compose.dev.yaml's healthcheck:
    block fully overrides it whenever compose *is* used, so nothing else
    would ever catch this drifting away from the fix above."""
    dockerfile = (REPO_ROOT / "docker" / "Dockerfile").read_text()
    dev_stage = dockerfile.split("FROM base AS dev", 1)[1].split("\nFROM ", 1)[0]
    match = re.search(
        r"HEALTHCHECK --interval=(\S+) --timeout=(\S+) --retries=(\d+) "
        r"--start-period=\S+\s*\\?\s*\n\s*CMD\s+(.+)",
        dev_stage,
    )
    assert match, "dev-stage HEALTHCHECK not found in the expected form"
    interval, timeout, retries, cmd = match.groups()

    max_time_match = re.search(r"--max-time\s+(\S+)", cmd)
    assert max_time_match, "curl must bound its own request"
    max_time = float(max_time_match.group(1))
    assert 0 < max_time < _seconds(timeout)

    window = _seconds(interval) * int(retries)
    assert window <= _MAX_DETECTION_WINDOW_S, (
        f"a dead dev worker takes {window}s to be reported unhealthy"
    )
