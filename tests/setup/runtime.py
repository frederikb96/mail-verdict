"""
Container runtime bootstrap for the pg and e2e test layers.

Rootless podman does not populate DOCKER_HOST or run a ryuk-compatible
reaper the way a Docker daemon does. This module makes testcontainers work
against it without any per-developer shell setup, and fails loudly with the
exact fix command when no runtime is reachable at all -- a test layer that
needs containers must never silently skip for their absence.
"""

from __future__ import annotations

import os
from pathlib import Path

PODMAN_SOCKET_ENV = "XDG_RUNTIME_DIR"


class ContainerRuntimeError(Exception):
    """Raised when no usable container runtime is found."""


DOCKER_DEFAULT_SOCKET = Path("/var/run/docker.sock")


def bootstrap_container_runtime() -> None:
    """
    Ensure a container runtime is reachable before any testcontainers fixture starts.

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
        return

    if DOCKER_DEFAULT_SOCKET.exists():
        return

    runtime_dir = os.environ.get(PODMAN_SOCKET_ENV)
    if runtime_dir:
        socket_path = Path(runtime_dir) / "podman" / "podman.sock"
        if socket_path.exists():
            os.environ["DOCKER_HOST"] = f"unix://{socket_path}"
            # Rootless podman has no ryuk-compatible reaper; container
            # cleanup falls back to testcontainers' own fixture teardown.
            os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")
            return

    raise ContainerRuntimeError(
        "No container runtime found. This test layer requires one -- set "
        "DOCKER_HOST explicitly, or enable the rootless podman socket:\n\n"
        "    systemctl --user enable --now podman.socket\n"
    )
