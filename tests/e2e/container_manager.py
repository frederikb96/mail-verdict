"""
E2E container lifecycle manager.

Manages podman compose services for E2E testing. Handles startup, shutdown,
health polling, log retrieval, and volume cleanup.

CRITICAL: Only manages containers within the mv-test project. Never touches
other containers or runs broad podman commands.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

TEMP_DATA_DIRS = [
    Path("/tmp/mv-test-pgdata"),
    Path("/tmp/mv-test-qdrant"),
    Path("/tmp/mv-test-stalwart"),
]


class ContainerManager:
    """
    Podman compose lifecycle manager for E2E test containers.

    Follows Prism's ContainerManager pattern:
    down() -> clean_volumes() -> up() -> wait_healthy()
    """

    def __init__(
        self,
        compose_file: Path,
        project_name: str = "mv-test",
    ) -> None:
        """
        Initialize the container manager.

        Args:
            compose_file: Path to the compose.test.yaml file
            project_name: Podman compose project name
        """
        self._compose_file = compose_file
        self._project_name = project_name

    def _run(
        self,
        args: list[str],
        *,
        check: bool = True,
        capture: bool = True,
        timeout: int = 120,
    ) -> subprocess.CompletedProcess[str]:
        """
        Run a podman compose command.

        Args:
            args: Arguments to pass after 'podman compose -f <file> -p <project>'
            check: Raise on non-zero exit
            capture: Capture stdout/stderr
            timeout: Command timeout in seconds

        Returns:
            Completed process result
        """
        cmd = [
            "podman", "compose",
            "-f", str(self._compose_file),
            "-p", self._project_name,
            *args,
        ]
        logger.debug("Running: %s", " ".join(cmd))
        return subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=check,
            timeout=timeout,
        )

    def down(self, timeout: int = 5) -> None:
        """
        Stop and remove all project containers.

        Args:
            timeout: Seconds to wait for graceful shutdown before killing
        """
        logger.info("Stopping containers (project=%s)", self._project_name)
        self._run(["down", "-t", str(timeout)], check=False)

    def clean_volumes(self) -> None:
        """Remove temp data directories used by test containers."""
        for path in TEMP_DATA_DIRS:
            if path.exists():
                logger.info("Removing %s", path)
                shutil.rmtree(path)

    def up(self, *, build: bool = True) -> None:
        """
        Start all project containers in detached mode.

        Args:
            build: Whether to rebuild images before starting
        """
        logger.info("Starting containers (project=%s)", self._project_name)
        args = ["up", "-d"]
        if build:
            args.append("--build")
        self._run(args, timeout=300)

    def wait_healthy(
        self,
        services: list[str] | None = None,
        timeout: int = 90,
        poll_interval: float = 2.0,
    ) -> None:
        """
        Wait until all specified containers report healthy status.

        Args:
            services: Container names to check. Defaults to all project containers.
            timeout: Maximum seconds to wait
            poll_interval: Seconds between health polls

        Raises:
            TimeoutError: If containers don't become healthy within timeout
        """
        if services is None:
            services = [
                "mv-postgres-test",
                "mv-qdrant-test",
                "mv-stalwart-test",
                "mv-app-test",
            ]

        deadline = time.monotonic() + timeout
        pending = set(services)

        while pending and time.monotonic() < deadline:
            for container in list(pending):
                status = self._get_health_status(container)
                if status == "healthy":
                    logger.info("Container healthy: %s", container)
                    pending.discard(container)
                elif status == "unhealthy":
                    logs = self.logs(container)
                    raise RuntimeError(
                        f"Container {container} is unhealthy. Logs:\n{logs}"
                    )

            if pending:
                remaining = deadline - time.monotonic()
                logger.debug(
                    "Waiting for %s (%.0fs remaining)",
                    ", ".join(sorted(pending)),
                    remaining,
                )
                time.sleep(poll_interval)

        if pending:
            diag_lines = []
            for container in sorted(pending):
                status = self._get_health_status(container)
                logs_tail = self.logs(container, tail=20)
                diag_lines.append(
                    f"  {container}: status={status}\n  Last 20 log lines:\n{logs_tail}"
                )
            raise TimeoutError(
                f"Containers not healthy after {timeout}s: "
                f"{', '.join(sorted(pending))}\n" + "\n".join(diag_lines)
            )

    def _get_health_status(self, container: str) -> str:
        """
        Get the health status of a container via podman inspect.

        Args:
            container: Container name

        Returns:
            Health status string (healthy, unhealthy, starting, none, missing)
        """
        result = subprocess.run(
            [
                "podman", "inspect",
                "--format", "{{.State.Health.Status}}",
                container,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return "missing"
        return result.stdout.strip() or "none"

    def logs(self, service: str, *, tail: int | None = None) -> str:
        """
        Get logs from a service container.

        Args:
            service: Service name as defined in compose file
            tail: Limit to last N lines

        Returns:
            Log output as string
        """
        args = ["logs", service]
        if tail is not None:
            args.extend(["--tail", str(tail)])
        result = self._run(args, check=False)
        return result.stdout + result.stderr

    def is_running(self) -> bool:
        """
        Check if project containers exist and are running.

        Returns:
            True if at least one project container is running
        """
        result = self._run(["ps", "-q"], check=False)
        return bool(result.stdout.strip())
