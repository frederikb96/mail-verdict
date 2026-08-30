"""
MailVerdict ASGI Server.

Single app serving:
- /api/* — REST API (FastAPI routers)
- /api/events — SSE real-time updates
- /api/health, /api/health/live — health/readiness checks
- /mcp — MCP streamable-http endpoint (FastMCP)

PostIMAP handles all IMAP sync. MailVerdict is a pure PostgreSQL application.
The FastAPI root is built first, MCP is mounted underneath it (inverted from
a design where routes get inserted into the MCP app), and one lifespan
composes every component: database, settings, the postimap event listener,
and the spam/rules consumers that listener drives.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp

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
_queue_manager: Any | None = None
_embedding_components: Any | None = None
_pipeline_notifier: Any | None = None
_pipeline_reconciler: Any | None = None
_contract_ok: bool = False


def get_spam_processor() -> Any | None:
    """Get the global spam feedback listener (for the feedback endpoint)."""
    return _spam_processor


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Lifespan context manager -- initializes all components via DI."""
    global _postimap_listener, _spam_processor, _contract_ok
    global _queue_manager, _pipeline_notifier, _pipeline_reconciler
    global _embedding_components

    config = get_config()

    from mail_verdict.core.logging import setup_logging

    setup_logging(config.server.log_level)
    logger.info("MailVerdict server starting")

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

    dsn = parse_dsn_from_sqlalchemy_url(config.database.url)
    _pipeline_notifier = WorkQueueNotifier(dsn)
    await _pipeline_notifier.start()

    _queue_manager = init_queue_manager(db)
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

    _postimap_listener = PostimapListener(dsn)
    _postimap_listener.add_handler(_on_postimap_event)
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


def _build_fastapi() -> FastAPI:
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

    app.mount("/mcp", mcp_app)

    from mail_verdict.api.routes import all_routers

    api_router = FastAPI()
    for router in all_routers:
        api_router.include_router(router)

    @api_router.get("/health/live")
    async def health_live() -> JSONResponse:
        """Liveness: process is up. Never touches the database."""
        return JSONResponse(status_code=200, content={"status": "alive"})

    @api_router.get("/health")
    async def health() -> JSONResponse:
        """
        Readiness: database reachable and PostIMAP contract version confirmed.

        Re-checks the contract whenever it isn't confirmed yet, so a pod that
        started before PostIMAP finished its own migrations becomes ready as
        soon as the contract row appears, instead of staying unready forever.
        """
        global _contract_ok
        try:
            db = get_db_connection()
            db_ok = await db.health_check()
        except RuntimeError:
            db_ok = False

        if db_ok and not _contract_ok:
            _contract_ok = await _check_contract(db)

        ready = db_ok and _contract_ok
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ready" if ready else "not_ready",
                "database": "ok" if db_ok else "error",
                "postimap_contract": "ok" if _contract_ok else "not confirmed",
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
    app = _build_fastapi()

    ui_build_dir = Path(__file__).parent.parent.parent / "ui" / "build"
    if not ui_build_dir.exists():
        ui_build_dir = Path("/app/ui/build")

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
