"""
MailVerdict ASGI Server.

Single app serving:
- /api/* — REST API (FastAPI routers)
- /api/events — SSE real-time updates
- /api/health — readiness check
- /mcp — MCP streamable-http endpoint (FastMCP)

Liveness is not one of these routes. It is answered by a plain socket server
on its own port and its own thread (see `start_liveness_server` below),
because an async endpoint here shares the event loop with every other
handler -- a handler that blocks that loop blocks liveness along with it,
which is exactly the failure a liveness probe exists to catch.

PostIMAP handles all IMAP sync. MailVerdict is a pure PostgreSQL application.
The FastAPI root is built first, MCP is mounted underneath it (inverted from
a design where routes get inserted into the MCP app), and one lifespan
composes every component: database, settings, the postimap event listener,
and the spam/rules consumers that listener drives.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp

from mail_verdict import __version__
from mail_verdict.api.security_headers import (
    SecurityHeadersMiddleware,
    build_content_security_policy,
    compute_inline_script_hashes,
)
from mail_verdict.config import MCP_TRANSPORT, get_config
from mail_verdict.database import close_database, get_db_connection, init_database
from mail_verdict.postimap.contract import (
    ContractMismatchError,
    assert_contract_version,
    read_postimap_info,
)
from mail_verdict.postimap.listener import PostimapListener, parse_dsn_from_sqlalchemy_url
from mail_verdict.settings.credentials import (
    init_provider_credential_repo,
    reset_provider_credential_repo,
)
from mail_verdict.settings.service import init_settings_service, reset_settings_service

logger = logging.getLogger(__name__)

_postimap_listener: PostimapListener | None = None
_spam_processor: Any | None = None
_calendar_intake_handler: Any | None = None
_queue_manager: Any | None = None
_embedding_components: Any | None = None
_pipeline_notifier: Any | None = None
_pipeline_reconciler: Any | None = None
_contract_ok: bool = False
_liveness_server: ThreadingHTTPServer | None = None
_liveness_thread: Thread | None = None


class _LivenessRequestHandler(BaseHTTPRequestHandler):
    """Answers every request 200, from a thread with its own listening
    socket -- there is nothing here for a blocked event loop to hold up."""

    def do_GET(self) -> None:
        body = b'{"status": "alive"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, log_format: str, *args: Any) -> None:
        """Silence the default per-request stderr log -- a probe hits this
        every few seconds for the life of the pod."""


def start_liveness_server(host: str, port: int) -> tuple[ThreadingHTTPServer, Thread]:
    """
    Start the liveness listener: its own socket, its own thread, no asyncio.

    A `ThreadingHTTPServer` so one slow or stuck client can never make the
    next probe queue behind it. Called from `lifespan` below, deliberately
    as its very first action, so liveness is already answering before
    anything that could be slow -- the database connection among it -- has
    even been attempted.

    Args:
        host: Bind address, `config.server.host`.
        port: TCP port to listen on, `config.server.liveness_port`.

    Returns:
        The server and the daemon thread running its `serve_forever()`.
    """
    server = ThreadingHTTPServer((host, port), _LivenessRequestHandler)
    thread = Thread(target=server.serve_forever, daemon=True, name="liveness-server")
    thread.start()
    return server, thread


def stop_liveness_server(server: ThreadingHTTPServer, thread: Thread) -> None:
    """Stop the liveness listener started by `start_liveness_server`."""
    server.shutdown()
    server.server_close()
    thread.join(timeout=1.0)


def get_spam_processor() -> Any | None:
    """Get the global spam feedback listener (for the feedback endpoint)."""
    return _spam_processor


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifespan context manager -- initializes all components via DI."""
    global _postimap_listener, _spam_processor, _contract_ok
    global _queue_manager, _pipeline_notifier, _pipeline_reconciler
    global _embedding_components, _calendar_intake_handler
    global _liveness_server, _liveness_thread

    config = get_config()

    from mail_verdict.core.logging import setup_logging

    setup_logging(config.server.log_level)
    logger.info("MailVerdict server starting")

    # Started first and stopped last, deliberately outside everything else
    # in this function -- liveness must answer for as long as the process
    # is up, including while the rest of startup (the database connection
    # among it) is still in progress or has failed outright.
    _liveness_server, _liveness_thread = start_liveness_server(
        config.server.host, config.server.liveness_port
    )
    logger.info("Liveness listener started on port %d", config.server.liveness_port)

    await init_database(config.database)
    logger.info("Database initialized")

    db = get_db_connection()
    settings_service = await init_settings_service(db)
    logger.info("Settings loaded from DB")

    cred_repo = init_provider_credential_repo(db, config.security.encryption_key)
    logger.info(
        "Provider credential storage ready"
        if config.security.encryption_key
        else "No ENCRYPTION_KEY set -- provider keys must come from environment variables",
    )

    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.api.events import init_event_ring

    event_ring = EventRing()
    init_event_ring(event_ring)
    logger.info("EventRing initialized")

    # The feedback listener is always constructed -- it is not gated on a
    # setting at startup, since the pipeline itself (and its classify
    # stage's own account_spam_enabled check) is what decides whether
    # anything gets classified; the listener only ever reacts to a folder
    # move, and a move can happen whether or not spam detection is on.
    from mail_verdict.database.repository import (
        AccountPrefsRepository,
        FolderRepository,
        VerdictRepository,
    )
    from mail_verdict.embeddings.worker import register_embeddings
    from mail_verdict.pipeline.enqueue import (
        build_reconciliation_timer,
        enqueue_live_arrival,
        record_folder_watermark,
    )
    from mail_verdict.pipeline.runner import PipelineRunner
    from mail_verdict.queue.manager import init_queue_manager
    from mail_verdict.queue.notify import WorkQueueNotifier
    from mail_verdict.spam.feedback import SpamFeedbackHandler
    from mail_verdict.spam.processor import SpamFeedbackListener

    verdict_repo = VerdictRepository(db)
    folder_repo = FolderRepository(db)
    account_prefs_repo = AccountPrefsRepository(db)
    feedback = SpamFeedbackHandler(verdict_repo)
    _spam_processor = SpamFeedbackListener(feedback=feedback, folder_repo=folder_repo)
    logger.info("Spam feedback listener initialized")

    # Constructed unconditionally too, the same reasoning as the spam
    # feedback listener -- calendar/intake.py's own message/insert gate
    # is what decides whether anything gets imported, not a setting here.
    from mail_verdict.calendar.intake import CalendarIntakeHandler
    from mail_verdict.calendar.repository import (
        CalendarIntakeRepository,
        CalendarPrefsRepository,
        CollectionRepository,
        DavAccountRepository,
        DavObjectRepository,
    )

    _calendar_intake_handler = CalendarIntakeHandler(
        db, CalendarIntakeRepository(db), DavObjectRepository(db),
        CalendarPrefsRepository(db), CollectionRepository(db), DavAccountRepository(db),
    )
    logger.info("Calendar intake handler initialized")

    dsn = parse_dsn_from_sqlalchemy_url(config.database.url)
    _pipeline_notifier = WorkQueueNotifier(dsn)
    await _pipeline_notifier.start()

    _queue_manager = init_queue_manager(
        db, reserved_for_requests=config.database.reserved_for_requests,
    )
    pipeline_runner = PipelineRunner(
        db, settings_service, cred_repo, account_prefs_repo, event_ring, _pipeline_notifier,
    )
    pipeline_runner.register(_queue_manager)

    # Registered before the manager starts, so its worker supervisor comes up
    # with the rest. Embedding is upstream of the pipeline: a message is
    # encoded first and only then becomes eligible for a run, so both queues
    # have to be running for anything to move.
    _embedding_components = register_embeddings(
        _queue_manager, db, cred_repo, settings_service,
    )

    await _queue_manager.start()
    await _embedding_components.start()
    logger.info("Pipeline and embedding queues registered and started")

    _pipeline_reconciler = build_reconciliation_timer(db, settings_service)
    await _pipeline_reconciler.start()

    async def _on_postimap_event(event: Any) -> None:
        """Dispatch a parsed postimap_events payload to EventRing, the
        pipeline's live-arrival enqueue, and the spam feedback listener."""
        import uuid as _uuid

        try:
            account_uuid = _uuid.UUID(event.account_id)
        except ValueError:
            return

        if event.type == "message":
            sse_data = {
                "id": event.id, "account_id": event.account_id, "folder_id": event.folder_id,
            }
            if event.op == "insert":
                await event_ring.add(account_uuid, "mail.new", sse_data)
                if event.origin == "sync":
                    await enqueue_live_arrival(db, event, settings_service)
                    if _calendar_intake_handler:
                        await _calendar_intake_handler.handle_message_event(event)
            elif event.op == "update":
                await event_ring.add(
                    account_uuid, "mail.updated", {**sse_data, "changed": list(event.changed)},
                )
                if _spam_processor:
                    await _spam_processor.handle_message_event(event)
            elif event.op == "delete":
                await event_ring.add(account_uuid, "mail.deleted", sse_data)

        elif event.type == "folder":
            if event.op == "sync_complete":
                await event_ring.add(
                    account_uuid, "folder.synced",
                    {"folder_id": event.folder_id, "backfill": event.backfill},
                )
                await record_folder_watermark(db, event)
            else:
                await event_ring.add(account_uuid, "folder.changed", {"folder_id": event.folder_id})

        elif event.type == "account":
            await event_ring.add(account_uuid, "account.changed", {"id": event.id, "op": event.op})

        elif event.type == "outbox":
            outbox_data = await _outbox_event_payload(db, event)
            await event_ring.add(account_uuid, "outbox.updated", outbox_data)

        elif event.type == "notification":
            await event_ring.add(
                account_uuid, "notification.new",
                {"id": event.id, "account_id": event.account_id},
            )

        elif event.type == "dav_account":
            await event_ring.add(
                account_uuid, "calendar.account", {"dav_account_id": event.account_id},
            )

        elif event.type == "dav_collection":
            kind = await _dav_collection_kind(db, event.id)
            sse_type = "contact.collection" if kind == "addressbook" else "calendar.collection"
            payload: dict[str, Any] = {"dav_account_id": event.account_id}
            payload["addressbook_id" if kind == "addressbook" else "calendar_id"] = event.id
            await event_ring.add(account_uuid, sse_type, payload)

        elif event.type == "dav_object":
            kind = (
                await _dav_collection_kind(db, event.collection_id)
                if event.collection_id else None
            )
            sse_type = "contact.object" if kind == "addressbook" else "calendar.object"
            payload = {"id": event.id, "dav_account_id": event.account_id}
            collection_key = "addressbook_id" if kind == "addressbook" else "calendar_id"
            payload[collection_key] = event.collection_id
            await event_ring.add(account_uuid, sse_type, payload)

        elif event.type == "dav_notification":
            await event_ring.add(
                account_uuid, "notification.new",
                {
                    "id": event.id, "account_id": event.account_id,
                    "dav_account_id": event.account_id,
                },
            )

    async def _on_postimap_reconnect() -> None:
        from mail_verdict.api.events import broadcast_resync

        await broadcast_resync(db, event_ring)

    _postimap_listener = PostimapListener(dsn)
    _postimap_listener.add_handler(_on_postimap_event)
    _postimap_listener.add_reconnect_handler(_on_postimap_reconnect)
    await _postimap_listener.start()
    logger.info("PostIMAP event listener started")

    _contract_ok = await _check_contract(db)

    yield

    logger.info("MailVerdict server shutting down")

    if _postimap_listener:
        await _postimap_listener.stop()
    if _pipeline_reconciler:
        await _pipeline_reconciler.stop()
    if _embedding_components:
        await _embedding_components.stop()
    if _queue_manager:
        await _queue_manager.stop()
    if _pipeline_notifier:
        await _pipeline_notifier.stop()

    _spam_processor = None
    _queue_manager = None
    _embedding_components = None
    _pipeline_notifier = None
    _pipeline_reconciler = None
    _contract_ok = False

    from mail_verdict.core.anthropic_provider import reset_anthropic_provider
    from mail_verdict.core.openai_provider import reset_openai_provider

    reset_anthropic_provider()
    reset_openai_provider()
    reset_provider_credential_repo()
    reset_settings_service()
    await close_database()
    logger.info("Database connection closed")

    _postimap_listener = None

    # Stopped last -- liveness should keep answering through every step
    # above, including one that hangs or raises.
    if _liveness_server and _liveness_thread:
        stop_liveness_server(_liveness_server, _liveness_thread)
    _liveness_server = None
    _liveness_thread = None


async def _check_contract(db: Any) -> bool:
    """
    Assert PostIMAP's contract_version at startup.

    A version mismatch is fatal (raises); a missing postimap_info row
    (PostIMAP hasn't finished its own migrations yet) is not fatal here --
    readiness just stays false until it appears, retried by the probe.

    Args:
        db: The initialized DatabaseConnection

    Returns:
        True if the contract version matches
    """
    try:
        async with db.session() as session:
            info = await read_postimap_info(session)
    except Exception:
        logger.warning("Could not read postimap_info yet", exc_info=True)
        return False

    if info is None:
        logger.warning("postimap_info has no row yet -- PostIMAP has not migrated")
        return False

    try:
        assert_contract_version(info)
    except ContractMismatchError:
        logger.exception("PostIMAP contract version mismatch")
        raise

    logger.info(
        "PostIMAP contract version confirmed",
        extra={"contract_version": info.contract_version, "service_version": info.service_version},
    )
    return True


async def _outbox_event_payload(db: Any, event: Any) -> dict[str, Any]:
    """
    Build the outbox.updated SSE payload, re-reading the row's current status/kind.

    The raw NOTIFY only names which columns changed, never their new values
    -- the UI's send/fail/dead toasts and the Sent-folder refresh all key
    off the current status, so it's re-read here rather than forwarded raw.

    Args:
        db: The initialized DatabaseConnection
        event: The parsed outbox PostimapEvent

    Returns:
        SSE payload with id and changed, plus status and kind when the row
        still exists
    """
    from sqlalchemy import select

    from mail_verdict.database.models import Outbox

    data: dict[str, Any] = {"id": event.id, "changed": list(event.changed)}
    try:
        outbox_id = uuid.UUID(event.id)
    except ValueError:
        return data

    async with db.session() as session:
        result = await session.execute(
            select(Outbox.status, Outbox.kind).where(Outbox.id == outbox_id)
        )
        row = result.one_or_none()
    if row is not None:
        data["status"] = row.status
        data["kind"] = row.kind
    return data


async def _dav_collection_kind(db: Any, collection_id: str) -> str | None:
    """
    Whether a dav_collections row is a calendar or an address book --
    what tells a dav_collection/dav_object event apart as calendar.* or
    contact.* on the wire. None if the id doesn't parse or the row is
    already gone (a delete event's own collection can outlive the row it
    named, so a missing kind is not an error here).
    """
    from sqlalchemy import select

    from mail_verdict.database.models import DavCollection

    try:
        collection_uuid = uuid.UUID(collection_id)
    except ValueError:
        return None

    async with db.session() as session:
        kind: str | None = await session.scalar(
            select(DavCollection.kind).where(DavCollection.id == collection_uuid)
        )
    return kind


def _resolve_ui_build_dir() -> Path:
    """Where the built UI lives, in a checkout or in the container image."""
    ui_build_dir = Path(__file__).parent.parent.parent / "ui" / "build"
    if not ui_build_dir.exists():
        ui_build_dir = Path("/app/ui/build")
    return ui_build_dir


def _build_fastapi(ui_build_dir: Path) -> FastAPI:
    """Build the FastAPI root app: MCP mount, API routers, SSE route, health."""
    from mail_verdict.api.mcp_tools import mcp as mcp_server

    # FastMCP's session manager runs its own lifespan; mounting it under a
    # parent app does not invoke that lifespan automatically, so the
    # combined lifespan below wraps our own init/teardown inside it.
    mcp_app = mcp_server.http_app(path="/", transport=MCP_TRANSPORT)  # type: ignore[arg-type]

    @asynccontextmanager
    async def combined_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with mcp_app.lifespan(app):
            async with lifespan(app):
                yield

    # MailVerdict has no auth layer of its own: the deployment model is an
    # authenticating proxy in front of it (see README). Nothing here checks
    # a header or a key.
    app = FastAPI(title="MailVerdict", lifespan=combined_lifespan)

    config = get_config()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.server.cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    script_hashes = compute_inline_script_hashes(ui_build_dir)
    app.add_middleware(
        SecurityHeadersMiddleware,
        content_security_policy=build_content_security_policy(script_hashes),
    )

    # Registered before the mount, because a Mount claims every path at and
    # below its prefix and a route added afterwards is simply unreachable.
    # The mount answers /mcp/ but not /mcp, which is the address the README
    # and every client config give -- and the failure is a 405, which reads
    # as a wrong method rather than a missing slash. 307 preserves the POST
    # and its body.
    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
    async def _mcp_slash_redirect(request: Request) -> RedirectResponse:
        return RedirectResponse(url="/mcp/", status_code=307)

    app.mount("/mcp", mcp_app)

    from mail_verdict.api.routes import all_routers

    # Read from the installed distribution rather than restated here, so the
    # served document cannot drift from the package it describes.
    api_router = FastAPI(title="MailVerdict API", version=__version__)
    for router in all_routers:
        api_router.include_router(router)

    @api_router.get("/health")
    async def health() -> JSONResponse:
        """
        Readiness: the PostIMAP contract is confirmed and the database answers.

        Both checks are bounded by `config.server.readiness_timeout_seconds`,
        and a timeout is treated as busy rather than broken -- a loaded pod
        stays in the Service, which matters at one replica. Only an explicit
        failure takes it out: a database that answers with an error, or that
        was never initialised, is a pod that can serve nothing but 500s.

        Liveness is a separate socket on its own thread, so a slow answer here
        can never get the process restarted.
        """
        global _contract_ok

        timeout = config.server.readiness_timeout_seconds
        db_state = "ok"

        try:
            db = get_db_connection()
            if not _contract_ok:
                _contract_ok = await asyncio.wait_for(_check_contract(db), timeout=timeout)
            elif not await asyncio.wait_for(db.health_check(), timeout=timeout):
                db_state = "unreachable"
        except asyncio.TimeoutError:
            # Busy, not broken. Whatever was last established still stands.
            db_state = "slow"
            logger.debug("Readiness probe timed out after %ss", timeout)
        except RuntimeError as e:
            db_state = "unreachable"
            logger.warning("Readiness probe: database not initialised: %s", e)
        except Exception as e:
            db_state = "unreachable"
            logger.warning("Readiness probe error: %s", e, exc_info=False)

        ready = _contract_ok and db_state != "unreachable"
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not_ready",
                "postimap_contract": "ok" if _contract_ok else "not confirmed",
                "database": db_state,
            },
        )

    from mail_verdict.api.events import sse_endpoint

    # Registered before the /api mount, because Starlette matches routes in
    # order and a Mount claims every path beneath it: appended afterwards,
    # this route is unreachable and the endpoint answers 404.
    app.router.routes.append(Route("/api/events", sse_endpoint))

    app.mount("/api", api_router)

    return app


def create_app() -> ASGIApp:
    """Create the MailVerdict ASGI application."""
    ui_build_dir = _resolve_ui_build_dir()
    app = _build_fastapi(ui_build_dir)

    if ui_build_dir.exists():
        next_dir = ui_build_dir / "_next"
        if next_dir.exists():
            app.router.routes.append(
                Mount("/_next", app=StaticFiles(directory=str(next_dir)), name="next-assets")
            )

        async def spa_fallback(request: Any) -> FileResponse | JSONResponse:
            """Serve pre-rendered pages and SPA fallback."""
            path = request.path_params.get("path", "")
            if path.startswith("api/") or path.startswith("mcp"):
                return JSONResponse(status_code=404, content={"detail": "Not found"})
            if path:
                # Both candidates get the same containment check. A request
                # path is attacker-controlled, and appending a suffix to it
                # does not make it safe -- ".." still climbs, it just lands
                # on a different file.
                root = ui_build_dir.resolve()

                def _within(candidate: Path) -> bool:
                    """Whether a candidate resolves inside the build directory."""
                    try:
                        return candidate.resolve().is_relative_to(root)
                    except OSError:
                        return False

                exact_file = ui_build_dir / path
                if exact_file.is_file() and _within(exact_file):
                    return FileResponse(str(exact_file))
                page_html = ui_build_dir / f"{path}.html"
                if page_html.is_file() and _within(page_html):
                    return FileResponse(str(page_html))
            index = ui_build_dir / "index.html"
            if index.exists():
                return FileResponse(str(index))
            return JSONResponse(status_code=404, content={"detail": "Not found"})

        app.router.routes.append(Route("/{path:path}", spa_fallback))
        logger.info("Static UI served from %s", ui_build_dir)

    logger.info("MCP enabled at /mcp (transport=%s)", MCP_TRANSPORT)
    return app
