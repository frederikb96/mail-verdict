"""
MailVerdict ASGI Server.

Single app serving:
- /api/* — REST API (FastAPI routers)
- /api/events — SSE real-time updates
- /api/health — Health check
- /mcp — MCP streamable-http endpoint (FastMCP)

When MCP is enabled, follows engram's single-container pattern:
FastMCP creates a Starlette app, all REST API routes from FastAPI
are inserted at position 0 (priority over MCP catch-all), and this
single composed Starlette app is served on one port.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp

from mail_verdict.config import MCP_TRANSPORT, get_config
from mail_verdict.database import close_database, get_db_connection, init_database
from mail_verdict.jobs.manager import init_job_manager, reset_job_manager
from mail_verdict.rules.bus import EventBus
from mail_verdict.rules.engine import RulesEngine
from mail_verdict.rules.enrichment import EnrichmentRunner
from mail_verdict.rules.executor import ActionExecutor
from mail_verdict.semantic.store import SemanticStore
from mail_verdict.semantic.worker import (
    init_embedding_worker,
    reset_embedding_worker,
)
from mail_verdict.settings.service import init_settings_service, reset_settings_service
from mail_verdict.spam.processor import SpamEventProcessor
from mail_verdict.sync.actions import ActionPropagator
from mail_verdict.sync.engine import SyncEngine

logger = logging.getLogger(__name__)

_qdrant_client: Any | None = None
_sync_engine: SyncEngine | None = None
_spam_processor: SpamEventProcessor | None = None
_event_bus: EventBus | None = None
_rules_engine: RulesEngine | None = None


def _get_qdrant_client() -> Any:
    """Get the Qdrant client singleton."""
    if _qdrant_client is None:
        raise RuntimeError("Server not initialized")
    return _qdrant_client


def get_event_bus() -> EventBus | None:
    """Get the global event bus (for SSE subscriber registration)."""
    return _event_bus


def get_spam_processor() -> SpamEventProcessor | None:
    """Get the global spam processor (for feedback endpoint)."""
    return _spam_processor


def get_sync_engine() -> SyncEngine | None:
    """Get the global sync engine."""
    return _sync_engine


@asynccontextmanager
async def lifespan(app: Starlette | FastAPI) -> AsyncIterator[None]:
    """
    Lifespan context manager.

    Initializes all components via DI on startup, tears down on shutdown.
    Works with both Starlette and FastAPI app types.
    """
    global _qdrant_client

    config = get_config()

    from mail_verdict.core.logging import setup_logging

    setup_logging(config.server.log_level)
    logger.info("MailVerdict server starting")

    # Validate encryption key
    from mail_verdict.core.encryption import validate_key

    validate_key()
    logger.info("Encryption key validated")

    # Initialize database
    await init_database(config.database)
    logger.info("Database initialized")

    # Initialize settings from DB
    db = get_db_connection()
    settings_service = await init_settings_service(db)
    logger.info("Settings loaded from DB")

    # Read settings snapshots for component initialization
    from mail_verdict.core.retry import RetryConfig as RC

    ai_settings = settings_service.get("ai")
    spam_settings = settings_service.get("spam")
    retry_settings = settings_service.get("retry")
    retry_config = RC.from_settings(retry_settings)

    # Initialize OpenAI provider (dynamic: reads api_key from settings on each call)
    from mail_verdict.core.openai_provider import init_openai_provider, reset_openai_provider

    openai_provider = init_openai_provider(settings_service)
    if openai_provider.get_client():
        logger.info("OpenAI API key configured")
    else:
        logger.info("No OpenAI API key yet — set via Settings API")

    # Initialize Qdrant client
    from qdrant_client import AsyncQdrantClient

    _qdrant_client = AsyncQdrantClient(
        host=config.qdrant.host,
        port=config.qdrant.port,
    )
    logger.info("Qdrant client created")

    # Initialize SemanticStore + EmbeddingWorker (always init, gracefully handles missing key)
    store = SemanticStore.init_instance(
        _qdrant_client,
        config.qdrant,
        ai_settings,
    )
    await store.ensure_collection()
    logger.info("SemanticStore initialized")

    worker = init_embedding_worker()
    await worker.start()
    logger.info("EmbeddingWorker started")

    # Initialize job manager
    init_job_manager(db)
    logger.info("JobManager initialized")

    # Initialize event bus early so sync engine can emit to it
    global _event_bus, _rules_engine
    _event_bus = EventBus()

    # Initialize EventRing for SSE event buffering
    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.api.events import init_event_ring, set_tracker_accessor

    event_ring = EventRing()
    init_event_ring(event_ring)
    logger.info("EventRing initialized")

    # Start IMAP sync engine (receives event_bus + event_ring)
    global _sync_engine
    _sync_engine = SyncEngine(
        config, db, event_bus=_event_bus,
        settings_service=settings_service, event_ring=event_ring,
    )
    await _sync_engine.start()
    logger.info("Sync engine started")

    # Register tracker accessor for SSE state snapshots
    set_tracker_accessor(_sync_engine.get_tracker)

    # Initialize and start spam processing pipeline
    global _spam_processor
    if spam_settings.get("enabled", False):
        from mail_verdict.database.repository import (
            FolderRepository,
            MailRepository,
            VerdictRepository,
        )
        from mail_verdict.spam.analyst import OpenAISpamAnalyst
        from mail_verdict.spam.feedback import SpamFeedbackHandler
        from mail_verdict.spam.pipeline import VerdictPipeline

        analyst = OpenAISpamAnalyst(
            ai_settings, spam_settings, retry_config,
        )
        verdict_repo = VerdictRepository(db)
        mail_repo = MailRepository(db)
        folder_repo = FolderRepository(db)

        # Build event queues from sync engine accounts
        event_queues = {}
        for name, account_sync in _sync_engine._accounts.items():
            event_queues[name] = account_sync.manager.event_queue

        feedback = SpamFeedbackHandler(store, verdict_repo)

        if event_queues:
            spam_propagators: dict[str, ActionPropagator] = {}
            for name, account_sync in _sync_engine._accounts.items():
                spam_propagators[str(account_sync.account_id)] = account_sync.action_propagator

            first_pipeline = VerdictPipeline(
                settings_service=settings_service,
                semantic_store=store,
                analyst=analyst,
                verdict_repo=verdict_repo,
                mail_repo=mail_repo,
                account_propagators=spam_propagators,
                folder_repo=folder_repo,
            )
            _spam_processor = SpamEventProcessor(
                pipeline=first_pipeline,
                feedback=feedback,
                mail_repo=mail_repo,
                folder_repo=folder_repo,
            )
            await _spam_processor.start(event_queues)
            logger.info("Spam event processor started")
    else:
        logger.info("Spam detection disabled")

    # Initialize rules engine (with propagators wired into ActionExecutor)
    # Rules now stored in settings DB under 'rules' category (or empty)
    rules_data = settings_service.get("rules") if settings_service.has_category("rules") else {}
    rules_list = rules_data.get("rules", []) if isinstance(rules_data, dict) else []

    if rules_list:
        from mail_verdict.database.repository import FolderRepository as FR
        from mail_verdict.database.repository import TagRepository

        tag_repo = TagRepository(db)
        rules_folder_repo = FR(db)
        enrichment_runner = EnrichmentRunner(
            ai_provider=ai_settings.get("provider", "openai"),
            ai_model=ai_settings.get("model", "gpt-5-mini"),
        )

        # Build multi-account propagator map for the action executor
        account_propagators: dict[str, ActionPropagator] = {}
        for name, account_sync in _sync_engine._accounts.items():
            account_propagators[str(account_sync.account_id)] = account_sync.action_propagator

        default_propagator = next(iter(account_propagators.values()), None)

        action_executor = ActionExecutor(
            propagator=default_propagator,
            tag_repo=tag_repo,
            folder_repo=rules_folder_repo,
        )
        _rules_engine = RulesEngine(
            rules=rules_list,
            bus=_event_bus,
            action_executor=action_executor,
            enrichment_runner=enrichment_runner,
            db=db,
        )
        await _rules_engine.start()
        logger.info("Rules engine started")
    else:
        logger.info("No rules configured")

    # Register SSE subscriber on the event bus
    from mail_verdict.api.events import register_sse_subscriber

    await register_sse_subscriber(_event_bus)

    yield

    # Cleanup (reverse order)
    logger.info("MailVerdict server shutting down")

    if _rules_engine:
        await _rules_engine.stop()
        logger.info("Rules engine stopped")

    if _spam_processor:
        await _spam_processor.stop()
        logger.info("Spam event processor stopped")

    if _sync_engine:
        await _sync_engine.stop()
        logger.info("Sync engine stopped")

    try:
        from mail_verdict.semantic.worker import get_embedding_worker

        w = get_embedding_worker()
        await w.stop()
        logger.info("EmbeddingWorker stopped")
    except RuntimeError:
        pass
    reset_embedding_worker()

    SemanticStore.reset_instance()

    if _qdrant_client:
        await _qdrant_client.close()
        logger.info("Qdrant client closed")

    reset_job_manager()
    reset_openai_provider()
    reset_settings_service()
    await close_database()
    logger.info("Database connection closed")

    _qdrant_client = None
    _sync_engine = None
    _spam_processor = None
    _event_bus = None
    _rules_engine = None


def get_action_propagator(account_id: uuid.UUID) -> ActionPropagator | None:
    """
    Get the ActionPropagator for a specific account.

    Args:
        account_id: Account UUID
    """
    if _sync_engine is None:
        return None
    account_id_str = str(account_id)
    for _name, account_sync in _sync_engine._accounts.items():
        if str(account_sync.account_id) == account_id_str:
            return account_sync.action_propagator
    return None


def _build_fastapi() -> FastAPI:
    """Build the FastAPI sub-app with all REST routers and health endpoint."""
    from mail_verdict.api.auth import require_auth

    fastapi_app = FastAPI(
        title="MailVerdict",
        version="0.2.0",
        dependencies=[Depends(require_auth)],
    )

    from mail_verdict.api.routes import all_routers

    for router in all_routers:
        fastapi_app.include_router(router)

    @fastapi_app.get("/health", dependencies=[])
    async def health() -> JSONResponse:
        """Health check endpoint for K8s liveness/readiness probes."""
        dependencies: dict[str, str] = {}

        try:
            db = get_db_connection()
            db_ok = await db.health_check()
            dependencies["postgres"] = "ok" if db_ok else "error: health check failed"
        except Exception as exc:
            dependencies["postgres"] = f"error: {exc}"

        try:
            client = _get_qdrant_client()
            await client.get_collections()
            dependencies["qdrant"] = "ok"
        except Exception as exc:
            dependencies["qdrant"] = f"error: {exc}"

        try:
            if _sync_engine is not None and _sync_engine._accounts:
                dependencies["imap"] = "ok"
            elif _sync_engine is not None:
                dependencies["imap"] = "no accounts configured"
            else:
                dependencies["imap"] = "not initialized"
        except Exception as exc:
            dependencies["imap"] = f"error: {exc}"

        core_statuses = [v for k, v in dependencies.items() if k != "imap"]
        all_core_ok = bool(core_statuses) and all(v == "ok" for v in core_statuses)

        return JSONResponse(
            status_code=200 if all_core_ok else 503,
            content={
                "status": "healthy" if all_core_ok else "degraded",
                "dependencies": dependencies,
            },
        )

    return fastapi_app


def create_app() -> ASGIApp:
    """
    Create the MailVerdict ASGI application.

    When MCP is enabled, follows engram's single-container pattern:
    FastMCP.http_app() creates the base Starlette app (with /mcp endpoint),
    all REST API routes are inserted at position 0 for priority.

    When MCP is disabled, uses a standard FastAPI app.
    """
    config = get_config()
    fastapi_app = _build_fastapi()

    from mail_verdict.api.events import sse_endpoint
    from mail_verdict.api.mcp_tools import mcp as mcp_server

    # MCP creates the base Starlette app
    composed_app = mcp_server.http_app(
        path="/mcp",
        transport=MCP_TRANSPORT,  # type: ignore[arg-type]
    )

    composed_app.add_middleware(
        CORSMiddleware,
        allow_origins=config.server.cors_origins,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
    )

    # Mount entire FastAPI app at position 0 (preserves FastAPI middleware stack)
    composed_app.routes.insert(0, Mount("/api", app=fastapi_app, name="fastapi"))

    # SSE route must be BEFORE the /api mount so it's matched first
    composed_app.routes.insert(0, Route("/api/events", sse_endpoint))

    # Assign lifespan to the composed app
    composed_app.router.lifespan_context = lifespan  # type: ignore[attr-defined]

    # Serve static UI files if build directory exists
    ui_build_dir = Path(__file__).parent.parent.parent / "ui" / "build"
    if not ui_build_dir.exists():
        ui_build_dir = Path("/app/ui/build")

    if ui_build_dir.exists():
        # Serve Next.js static export assets
        next_dir = ui_build_dir / "_next"
        if next_dir.exists():
            composed_app.routes.append(
                Mount("/_next", app=StaticFiles(directory=str(next_dir)), name="next-assets")
            )
        async def spa_fallback(request: Any) -> FileResponse | JSONResponse:
            """Serve index.html for SPA, 404 for API/MCP paths."""
            path = request.path_params.get("path", "")
            if path.startswith("api/") or path.startswith("mcp"):
                return JSONResponse(status_code=404, content={"detail": "Not found"})
            # Try serving the exact path's HTML file first (pre-rendered pages)
            page_html = ui_build_dir / f"{path}.html" if path else None
            if page_html and page_html.exists():
                return FileResponse(str(page_html))
            index = ui_build_dir / "index.html"
            if index.exists():
                return FileResponse(str(index))
            return JSONResponse(status_code=404, content={"detail": "Not found"})

        composed_app.routes.append(Route("/{path:path}", spa_fallback))
        logger.info("Static UI served from %s", ui_build_dir)

    logger.info("MCP enabled at /mcp (transport=%s)", MCP_TRANSPORT)
    return composed_app
