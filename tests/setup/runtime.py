"""
Container runtime bootstrap for the pg and e2e test layers.

Rootless podman does not populate DOCKER_HOST or run a ryuk-compatible
reaper the way a Docker daemon does. This module makes testcontainers work
against it without any per-developer shell setup, and fails loudly with the
exact fix command when no runtime is reachable at all -- a test layer that
needs containers must never silently skip for their absence.

Ryuk being disabled here is not a degraded version of cleanup -- it is none.
Ryuk's entire purpose is removing what a *killed* process leaves behind (a
foreground command hitting a timeout, an agent stopped, a machine-wide OOM);
a process that exits normally already tears its own containers down via its
fixtures' or scripts/devstack.py's own context managers, Ryuk or not. Without
it, a killed run's containers simply sit there forever, indistinguishable
from a legitimately long-running stack by anything that looks at their age --
so `owner_labels`/`sweep_orphaned_containers` below identify them by whether
the specific process that started them is still alive instead.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PODMAN_SOCKET_ENV = "XDG_RUNTIME_DIR"

# Every container this project's test/dev tooling starts carries these,
# applied via DockerContainer.with_kwargs(labels=owner_labels()) in
# tests/setup/containers.py -- never on compose.dev.yaml's containers,
# which podman compose creates directly rather than through this API, so
# the persistent dev stack can never carry them and can never be swept.
LABEL_ROLE = "mail-verdict.role"
LABEL_OWNER_PID = "mail-verdict.owner-pid"
LABEL_OWNER_FINGERPRINT = "mail-verdict.owner-fingerprint"
ROLE_TEST = "test"


class ContainerRuntimeError(Exception):
    """Raised when no usable container runtime is found."""


DOCKER_DEFAULT_SOCKET = Path("/var/run/docker.sock")


def bootstrap_container_runtime() -> None:
    """
    Ensure a container runtime is reachable before any testcontainers fixture starts,
    then sweep whatever a previous, now-dead process left behind.

    Three cases, in order:
      1. DOCKER_HOST already set -- an explicit setting (CI, a remote
         runtime) always wins, left untouched.
      2. The standard Docker socket exists (a real Docker daemon, or a
         DinD CI runner) -- testcontainers finds it on its own; nothing to
         set.
      3. Neither of the above: probe the rootless podman socket location
         and point testcontainers at it explicitly, since podman does not
         populate DOCKER_HOST itself.

    Raises:
        ContainerRuntimeError: If none of the three apply, with the exact
            command to fix it.
    """
    if os.environ.get("DOCKER_HOST"):
        _sweep_and_report()
        return

    if DOCKER_DEFAULT_SOCKET.exists():
        _sweep_and_report()
        return

    runtime_dir = os.environ.get(PODMAN_SOCKET_ENV)
    if runtime_dir:
        socket_path = Path(runtime_dir) / "podman" / "podman.sock"
        if socket_path.exists():
            os.environ["DOCKER_HOST"] = f"unix://{socket_path}"
            # Rootless podman has no ryuk-compatible reaper; container
            # cleanup falls back to testcontainers' own fixture teardown
            # (or scripts/devstack.py's own) and, for whatever that missed
            # because its owning process never got to run it, the sweep
            # below.
            os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
            _sweep_and_report()
            return

    raise ContainerRuntimeError(
        "No container runtime found. This test layer requires one -- set "
        "DOCKER_HOST explicitly, or enable the rootless podman socket:\n\n"
        "    systemctl --user enable --now podman.socket\n"
    )


def _sweep_and_report() -> None:
    removed = sweep_orphaned_containers()
    if removed:
        print(
            f"Removed {len(removed)} container(s) orphaned by a dead process: "
            f"{', '.join(removed)}",
            file=sys.stderr,
        )


def process_fingerprint(pid: int) -> str | None:
    """
    A cheap fingerprint for whichever process currently holds `pid`, distinct
    from whatever the OS might later reuse that same PID for.

    `/proc/<pid>`'s own ctime marks when the kernel allocated that PID to the
    process presently holding it -- a later, unrelated process the OS hands
    the same PID to (PIDs wrap and get reused) gets a different one, so a
    fingerprint recorded at container-creation time that no longer matches
    means the original owner is provably gone, not just that the PID number
    happens to be free. None if nothing holds `pid` at all right now.
    """
    try:
        return str(os.stat(f"/proc/{pid}").st_ctime)
    except (FileNotFoundError, ProcessLookupError, NotADirectoryError, PermissionError):
        return None


def owner_labels() -> dict[str, str]:
    """
    Labels every container this process starts should carry.

    Keyed on this process's own fingerprint, not on when the container was
    created -- age is useless here, since a genuine orphan and a
    legitimately long-running development stack accumulate it identically.
    Whether the specific process that requested this container is still
    alive is the actual question, and the only one sweep_orphaned_containers
    asks.
    """
    pid = os.getpid()
    fingerprint = process_fingerprint(pid)
    assert fingerprint is not None, "a live process always has a /proc entry for its own pid"
    return {
        LABEL_ROLE: ROLE_TEST,
        LABEL_OWNER_PID: str(pid),
        LABEL_OWNER_FINGERPRINT: fingerprint,
    }


def sweep_orphaned_containers() -> list[str]:
    """
    Remove every container carrying this project's test-role label whose
    owning process is confirmed dead, and return the names of whatever was
    removed.

    "Confirmed dead" means process_fingerprint(owner_pid) no longer matches
    what was recorded at creation time -- never an age threshold, and never
    just "no PID with this number exists", since a reused PID with a
    mismatched fingerprint is exactly as dead as a missing one. A container
    whose owner labels are missing or unparseable (not one of ours, or from
    a version of this tooling that predates labelling) is left alone rather
    than guessed at.
    """
    from docker.errors import NotFound

    import docker

    client = docker.from_env()
    removed = []
    containers = client.containers.list(
        all=True, filters={"label": f"{LABEL_ROLE}={ROLE_TEST}"},
    )
    for container in containers:
        owner_pid = container.labels.get(LABEL_OWNER_PID)
        owner_fingerprint = container.labels.get(LABEL_OWNER_FINGERPRINT)
        if owner_pid is None or owner_fingerprint is None:
            continue
        try:
            pid = int(owner_pid)
        except ValueError:
            continue
        if process_fingerprint(pid) == owner_fingerprint:
            continue  # the process that started this container is still alive

        name = container.name
        try:
            container.remove(force=True)
        except NotFound:
            continue
        removed.append(name)
    return removed
