#!/usr/bin/env python3
"""Bring up an ephemeral, compose-less development stack for one worktree.

compose.dev.yaml's fixed container names, host ports, and pgdata bind mount
mean only one instance of it can exist on a machine at a time, and two
worktrees on different Alembic revisions cannot share its one database at
all -- the app runs `alembic upgrade head` at startup and refuses on two
heads. This script solves both by building the same five containers
tests/setup/containers.py already gives the test suite (Postgres, Dovecot,
Mailpit, Radicale, PostIMAP) on a private network with random host ports,
migrating and starting *this worktree's* application against them, and
printing where to reach it. Run one per worktree; nothing is shared
between them.

    python scripts/devstack.py
    python scripts/devstack.py --to bob@test.local

Delivers the test corpus, creates and waits for the account, then blocks
until interrupted (Ctrl-C, or a `kill` of this process) -- both stop the
uvicorn server and tear down every container, cleanly and completely.

The signal has to reach *this* process. Running it through a pipe (`python
scripts/devstack.py | tee log`) or any other wrapper and then killing the
wrapper leaves this process an orphan, still running with its containers
still up -- a wrapper shell has no reason to forward a signal to a child
it never waited on. Find the actual `python scripts/devstack.py` PID
(`pgrep -f scripts/devstack.py`) and signal that one directly.

Teardown verifies its own work: every container this run started is
checked again, by ID, against the container runtime after the exit stack
has unwound, and force-removed if it is somehow still there. A run that
cannot confirm every container gone says so and exits non-zero instead of
printing "stopped" -- a stop that leaves a container running is not a
successful stop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from types import FrameType

import httpx
import uvicorn

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.seed_dev import DEFAULT_DAV_USER, seed_calendar  # noqa: E402
from tests.setup.containers import (  # noqa: E402
    DOVECOT_ALIAS,
    DOVECOT_IMAP_PORT,
    DOVECOT_LMTP_PORT,
    DOVECOT_PASSWORD,
    MAILPIT_ALIAS,
    MAILPIT_HTTP_PORT,
    MAILPIT_SMTP_PORT,
    RADICALE_ALIAS,
    RADICALE_PORT,
    build_dovecot_container,
    build_mailpit_container,
    build_postgres_container,
    build_postimap_container,
    build_radicale_container,
    postgres_url_for,
    wait_dovecot_ready,
    wait_mailpit_ready,
    wait_postimap_ready,
    wait_radicale_ready,
)
from tests.setup.mail_delivery import deliver_message, load_corpus  # noqa: E402
from tests.setup.migrations import run_migrations  # noqa: E402
from tests.setup.runtime import bootstrap_container_runtime  # noqa: E402

DEFAULT_RECIPIENT = "alice@test.local"
APP_READY_TIMEOUT_S = 30.0
ACCOUNT_ACTIVE_TIMEOUT_S = 60.0


def _get_random_port() -> int:
    """Get a random available port by binding to port 0 and reading back the assigned port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ThreadedUvicornServer(uvicorn.Server):
    """uvicorn.Server.install_signal_handlers() calls signal.signal(), which
    this script's own main-thread handler already owns -- overridden to a
    no-op, since shutdown here is driven by should_exit, not a signal."""

    def install_signal_handlers(self) -> None:
        pass


def _wait_until(condition: object, description: str, timeout_s: float) -> None:
    """Poll a zero-arg callable until it returns truthy, or raise naming what didn't happen."""
    assert callable(condition)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.5)
    raise TimeoutError(f"{description} did not happen within {timeout_s}s")


def _verify_torn_down(container_ids: dict[str, str]) -> list[str]:
    """Confirm every container this run started is actually gone, by asking the
    container runtime directly rather than trusting that the exit stack's own
    __exit__ calls landed. Force-removes anything still there and returns the
    names of whatever could not be removed even then -- empty means clean.

    This is the check the exit code alone cannot stand in for: a container
    whose stop() call itself raised (podman quirk, a runtime that was already
    gone, anything) does not stop the rest of the stack unwinding, so a run
    can print every "Starting ..." line, reach `stop.wait()`, and still leave
    a container running with nothing about the process's own exit revealing
    it.
    """
    from docker.errors import NotFound

    import docker

    client = docker.from_env()
    survivors = []
    for name, container_id in container_ids.items():
        try:
            container = client.containers.get(container_id)
        except NotFound:
            continue
        try:
            container.remove(force=True)
        except NotFound:
            continue
        except Exception:  # deliberately broad: any removal failure counts as a survivor
            survivors.append(name)
    return survivors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--to", default=DEFAULT_RECIPIENT,
        help="mailbox to seed with the test corpus and add as the account",
    )
    args = parser.parse_args()

    build_index = REPO_ROOT / "ui" / "build" / "index.html"
    if not build_index.exists():
        print(
            f"warning: {build_index} does not exist -- the API will run, but every page 404s. "
            "Build it with `cd ui && npm run build`.",
            file=sys.stderr,
        )

    bootstrap_container_runtime()

    stop = threading.Event()

    def _handle_signal(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # Populated as each container starts, read again after the exit stack has
    # unwound -- see _verify_torn_down.
    container_ids: dict[str, str] = {}

    try:
        _run(container_ids, args, stop)
    finally:
        # Runs whether the stack above unwound on Ctrl-C or on an exception
        # partway through startup -- a container that started before a later
        # one failed to come up is exactly the case most likely to leak, and
        # is also the case an exception propagating straight out of main()
        # would otherwise skip this check for entirely.
        survivors = _verify_torn_down(container_ids)
        if survivors:
            print(
                f"warning: {', '.join(survivors)} could not be confirmed removed -- check "
                "`podman ps` and remove manually.",
                file=sys.stderr,
            )
        else:
            print("Every container this run started is confirmed gone.")

    return 1 if survivors else 0


def _run(container_ids: dict[str, str], args: argparse.Namespace, stop: threading.Event) -> None:
    """The stack's actual startup, seeding and run -- factored out of main()
    so its `finally` can verify teardown regardless of how this returns."""
    with contextlib.ExitStack() as stack:
        from testcontainers.core.network import Network

        network = stack.enter_context(Network())

        print("Starting Postgres ...")
        postgres = stack.enter_context(build_postgres_container(network))
        container_ids["postgres"] = postgres.get_container_id()

        print("Starting Dovecot, Mailpit and Radicale ...")
        dovecot = stack.enter_context(build_dovecot_container(network))
        container_ids["dovecot"] = dovecot.get_container_id()
        wait_dovecot_ready(dovecot)
        mailpit = stack.enter_context(build_mailpit_container(network))
        container_ids["mailpit"] = mailpit.get_container_id()
        wait_mailpit_ready(mailpit)
        radicale = stack.enter_context(build_radicale_container(network))
        container_ids["radicale"] = radicale.get_container_id()
        wait_radicale_ready(radicale)

        print("Starting PostIMAP ...")
        postimap = stack.enter_context(build_postimap_container(network))
        container_ids["postimap"] = postimap.get_container_id()
        wait_postimap_ready(postimap)

        postgres_url = postgres_url_for(postgres)
        print("Running this worktree's migrations ...")
        asyncio.run(run_migrations(postgres_url))

        os.environ["MAIL_VERDICT_DATABASE_URL"] = postgres_url
        os.environ["MAIL_VERDICT_SERVER_LIVENESS_PORT"] = str(_get_random_port())
        from mail_verdict.config.loader import reset_config

        reset_config()

        from mail_verdict.server import create_app

        app = create_app()
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        server = _ThreadedUvicornServer(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        def _stop_app() -> None:
            server.should_exit = True
            thread.join(timeout=10)

        stack.callback(_stop_app)

        _wait_until(lambda: server.started, "uvicorn startup", APP_READY_TIMEOUT_S)
        port = server.servers[0].sockets[0].getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"

        def _app_ready() -> bool:
            try:
                return httpx.get(f"{base_url}/api/health", timeout=2.0).status_code == 200
            except httpx.HTTPError:
                return False

        _wait_until(_app_ready, "the app reaching /api/health", APP_READY_TIMEOUT_S)

        dovecot_host = dovecot.get_container_host_ip()
        dovecot_lmtp_port = int(dovecot.get_exposed_port(DOVECOT_LMTP_PORT))
        print(f"Delivering the test corpus to {args.to} ...")
        delivered = 0
        for _name, message in load_corpus():
            deliver_message(
                message, dovecot_host, dovecot_lmtp_port,
                sender="sender@example.com", recipient=args.to,
            )
            delivered += 1
        print(f"Delivered {delivered} messages.")

        api = httpx.Client(base_url=base_url, timeout=10.0)
        resp = api.post(
            "/api/accounts",
            json={
                "name": args.to,
                "imap_host": DOVECOT_ALIAS, "imap_port": DOVECOT_IMAP_PORT,
                "imap_user": args.to, "imap_password": DOVECOT_PASSWORD,
                "smtp_host": MAILPIT_ALIAS, "smtp_port": MAILPIT_SMTP_PORT,
                "smtp_user": args.to, "smtp_password": "unused",  # noqa: S106
            },
        )
        resp.raise_for_status()
        account_id = resp.json()["id"]

        print("Waiting for the account to sync ...")

        def _account_settled() -> bool:
            account = api.get(f"/api/accounts/{account_id}").json()
            if account["state"] == "error":
                raise RuntimeError(f"Account entered error state: {account['state_error']}")
            return bool(account["state"] == "active")

        _wait_until(_account_settled, "the account reaching 'active'", ACCOUNT_ACTIVE_TIMEOUT_S)

        radicale_host = radicale.get_container_host_ip()
        radicale_port = int(radicale.get_exposed_port(RADICALE_PORT))
        print("Seeding a calendar and address book on Radicale ...")
        seed_calendar(radicale_host, radicale_port, DEFAULT_DAV_USER)

        resp = api.post(
            "/api/dav-accounts",
            json={
                "name": DEFAULT_DAV_USER,
                "discovery_url": f"http://{RADICALE_ALIAS}:{RADICALE_PORT}/",
                "username": DEFAULT_DAV_USER,
                "password": "unused",  # noqa: S106
            },
        )
        resp.raise_for_status()
        dav_account_id = resp.json()["id"]

        print("Waiting for the DAV account to sync ...")

        def _dav_account_settled() -> bool:
            account = api.get(f"/api/dav-accounts/{dav_account_id}").json()
            if account["state"] == "error":
                raise RuntimeError(f"DAV account entered error state: {account['state_error']}")
            return bool(account["state"] == "active")

        _wait_until(
            _dav_account_settled, "the DAV account reaching 'active'", ACCOUNT_ACTIVE_TIMEOUT_S,
        )

        mailpit_port = int(mailpit.get_exposed_port(MAILPIT_HTTP_PORT))
        mailpit_url = f"http://{mailpit.get_container_host_ip()}:{mailpit_port}"

        print()
        print(f"MailVerdict:  {base_url}")
        print(f"Mailpit:      {mailpit_url}")
        print(f"Calendar:     {DEFAULT_DAV_USER!r}'s Personal calendar and Contacts address book")
        print()
        print("Ctrl-C, or a `kill` of this process, stops the stack and removes its containers.")

        stop.wait()
        print("Stopping ...")


if __name__ == "__main__":
    sys.exit(main())
