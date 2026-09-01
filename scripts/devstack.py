#!/usr/bin/env python3
"""Bring up an ephemeral, compose-less development stack for one worktree.

compose.dev.yaml's fixed container names, host ports, and pgdata bind mount
mean only one instance of it can exist on a machine at a time, and two
worktrees on different Alembic revisions cannot share its one database at
all -- the app runs `alembic upgrade head` at startup and refuses on two
heads. This script solves both by building the same four containers
tests/setup/containers.py already gives the test suite (Postgres, Dovecot,
Mailpit, PostIMAP) on a private network with random host ports, migrating
and starting *this worktree's* application against them, and printing where
to reach it. Run one per worktree; nothing is shared between them.

    python scripts/devstack.py
    python scripts/devstack.py --to bob@test.local

Delivers the test corpus, creates and waits for the account, then blocks
until interrupted (Ctrl-C, or a `kill` of this process) -- both stop the
uvicorn server and tear down every container, cleanly and completely.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import FrameType

import httpx
import uvicorn

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.setup.containers import (  # noqa: E402
    DOVECOT_ALIAS,
    DOVECOT_IMAP_PORT,
    DOVECOT_LMTP_PORT,
    DOVECOT_PASSWORD,
    MAILPIT_ALIAS,
    MAILPIT_HTTP_PORT,
    MAILPIT_SMTP_PORT,
    build_dovecot_container,
    build_mailpit_container,
    build_postgres_container,
    build_postimap_container,
    postgres_url_for,
    wait_dovecot_ready,
    wait_mailpit_ready,
    wait_postimap_ready,
)
from tests.setup.mail_delivery import deliver_message, load_corpus  # noqa: E402
from tests.setup.migrations import run_migrations  # noqa: E402
from tests.setup.runtime import bootstrap_container_runtime  # noqa: E402

DEFAULT_RECIPIENT = "alice@test.local"
APP_READY_TIMEOUT_S = 30.0
ACCOUNT_ACTIVE_TIMEOUT_S = 60.0


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

    with contextlib.ExitStack() as stack:
        from testcontainers.core.network import Network

        network = stack.enter_context(Network())

        print("Starting Postgres ...")
        postgres = stack.enter_context(build_postgres_container(network))

        print("Starting Dovecot and Mailpit ...")
        dovecot = stack.enter_context(build_dovecot_container(network))
        wait_dovecot_ready(dovecot)
        mailpit = stack.enter_context(build_mailpit_container(network))
        wait_mailpit_ready(mailpit)

        print("Starting PostIMAP ...")
        postimap = stack.enter_context(build_postimap_container(network))
        wait_postimap_ready(postimap)

        postgres_url = postgres_url_for(postgres)
        print("Running this worktree's migrations ...")
        asyncio.run(run_migrations(postgres_url))

        os.environ["MAIL_VERDICT_DATABASE_URL"] = postgres_url
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

        mailpit_port = int(mailpit.get_exposed_port(MAILPIT_HTTP_PORT))
        mailpit_url = f"http://{mailpit.get_container_host_ip()}:{mailpit_port}"

        print()
        print(f"MailVerdict:  {base_url}")
        print(f"Mailpit:      {mailpit_url}")
        print()
        print("Ctrl-C, or a `kill` of this process, stops the stack and removes its containers.")

        stop.wait()
        print("Stopping ...")

    return 0


if __name__ == "__main__":
    sys.exit(main())
