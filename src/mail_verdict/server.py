"""
MailVerdict ASGI Server.

Single app serving:
- /api/* — REST API (FastAPI routers)
- /api/events — SSE real-time updates
- /api/health — Health check
- /mcp — MCP streamable-http endpoint (FastMCP)

PostIMAP handles all IMAP sync. MailVerdict is a pure PostgreSQL application.
PG LISTEN/NOTIFY drives real-time events (SSE, spam pipeline, rules engine).
"""

from __future__ import annotations

import logging
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
from mail_verdict.database.pg_listener import PgListener, parse_dsn_from_sqlalchemy_url
from mail_verdict.semantic.store import SemanticStore
from mail_verdict.semantic.worker import (
    init_embedding_worker,
    reset_embedding_worker,
)
from mail_verdict.settings.service import init_settings_service, reset_settings_service

logger = logging.getLogger(__name__)

_qdrant_client: Any | None = None
_pg_listener: PgListener | None = None
_spam_processor: Any | None = None
_rules_engine: Any | None = None


def _get_qdrant_client() -> Any:
    """Get the Qdrant client singleton."""
    if _qdrant_client is None:
        raise RuntimeError("Server not initialized")
    return _qdrant_client


def get_spam_processor() -> Any | None:
    """Get the global spam processor (for feedback endpoint)."""
    return _spam_processor


@asynccontextmanager
async def lifespan(app: Starlette | FastAPI) -> AsyncIterator[None]:
    """Lifespan context manager — initializes all components via DI."""
    global _qdrant_client, _pg_listener, _spam_processor, _rules_engine

    config = get_config()

    from mail_verdict.core.logging import setup_logging

    setup_logging(config.server.log_level)
    logger.info("MailVerdict server starting")

    # Initialize database
    await init_database(config.database)
    logger.info("Database initialized")

    # Initialize settings from DB
    db = get_db_connection()
    settings_service = await init_settings_service(db)
    logger.info("Settings loaded from DB")

    # Read settings snapshots
    ai_settings = settings_service.get("ai")
    spam_settings = settings_service.get("spam")

    # Initialize OpenAI provider
    from mail_verdict.core.openai_provider import init_openai_provider, reset_openai_provider

    openai_provider = init_openai_provider(settings_service)
    if openai_provider.get_client():
        logger.info("OpenAI API key configured")
    else:
        logger.info("No OpenAI API key yet — set via Settings API")

    # Initialize Qdrant
    from qdrant_client import AsyncQdrantClient

    _qdrant_client = AsyncQdrantClient(
        host=config.qdrant.host,
        port=config.qdrant.port,
    )
    logger.info("Qdrant client created")

    store = SemanticStore.init_instance(_qdrant_client, config.qdrant, ai_settings)
    await store.ensure_collection()
    logger.info("SemanticStore initialized")

    worker = init_embedding_worker()
    await worker.start()
    logger.info("EmbeddingWorker started")

    # Initialize EventRing for SSE
    from mail_verdict.api.event_ring import EventRing
    from mail_verdict.api.events import init_event_ring

    event_ring = EventRing()
    init_event_ring(event_ring)
    logger.info("EventRing initialized")

    # Initialize spam processor (called by PG LISTEN on new messages)
    from mail_verdict.spam.processor import SpamEventProcessor

    if spam_settings.get("enabled", False):
        from mail_verdict.core.retry import RetryConfig as RC
        from mail_verdict.database.repository import (
            FolderRepository,
            MessageRepository,
            VerdictRepository,
        )
        from mail_verdict.spam.analyst import OpenAISpamAnalyst
        from mail_verdict.spam.feedback import SpamFeedbackHandler
        from mail_verdict.spam.pipeline import VerdictPipeline

        retry_config = RC.from_settings(settings_service.get("retry"))
        analyst = OpenAISpamAnalyst(ai_settings, spam_settings, retry_config)
        verdict_repo = VerdictRepository(db)
        message_repo = MessageRepository(db)
        folder_repo = FolderRepository(db)
        feedback = SpamFeedbackHandler(store, verdict_repo)

        pipeline = VerdictPipeline(
            settings_service=settings_service,
            semantic_store=store,
            analyst=analyst,
            verdict_repo=verdict_repo,
            message_repo=message_repo,
            folder_repo=folder_repo,
            db=db,
        )
        _spam_processor = SpamEventProcessor(
            pipeline=pipeline,
            feedback=feedback,
            message_repo=message_repo,
            folder_repo=folder_repo,
            db=db,
        )
        logger.info("Spam processor initialized")
    else:
        logger.info("Spam detection disabled")

    # Initialize rules engine (called by PG LISTEN on new messages)
    rules_data = settings_service.get("rules") if settings_service.has_category("rules") else {}
    rules_list = rules_data.get("rules", []) if isinstance(rules_data, dict) else []

    if rules_list:
        from mail_verdict.database.repository import FolderRepository as FR
        from mail_verdict.database.repository import TagRepository
        from mail_verdict.rules.engine import RulesEngine
        from mail_verdict.rules.enrichment import EnrichmentRunner
        from mail_verdict.rules.executor import ActionExecutor

        tag_repo = TagRepository(db)
        rules_folder_repo = FR(db)
        enrichment_runner = EnrichmentRunner(
            ai_provider=ai_settings.get("provider", "openai"),
            ai_model=ai_settings.get("model", "gpt-5-mini"),
            reasoning_effort=ai_settings.get("reasoning_effort"),
        )
        action_executor = ActionExecutor(
            tag_repo=tag_repo,
            folder_repo=rules_folder_repo,
        )
        _rules_engine = RulesEngine(
            rules=rules_list,
            action_executor=action_executor,
            enrichment_runner=enrichment_runner,
            db=db,
        )
        logger.info("Rules engine initialized")
    else:
        logger.info("No rules configured")

    # Start PG LISTEN dispatcher (replaces SyncEngine + OutboundProcessor)
    async def _on_pg_event(event: dict[str, Any]) -> None:
        """Dispatch PG NOTIFY events to EventRing, spam, and rules."""
        import uuid as _uuid

        channel = event.get("_channel", "")
        op = event.get("op", "")

        if channel == "mv_messages":
            account_id_str = event.get("account_id", "")
            msg_id = event.get("id", "")
            folder_id = event.get("folder_id", "")

            try:
                account_uuid = _uuid.UUID(account_id_str)
            except (ValueError, AttributeError):
                return

            if op == "insert":
                await event_ring.add(account_uuid, "mail.new", {
                    "id": msg_id, "account_id": account_id_str, "folder_id": folder_id,
                })
                if _spam_processor:
                    await _spam_processor.handle_message_event(event)
                if _rules_engine:
                    await _rules_engine.handle_message_event(event)
            elif op == "update":
                await event_ring.add(account_uuid, "mail.updated", {
                    "id": msg_id, "account_id": account_id_str, "folder_id": folder_id,
                    "old_folder_id": event.get("old_folder_id"),
                    "is_seen": event.get("is_seen"),
                    "is_flagged": event.get("is_flagged"),
                })
            elif op == "delete":
                await event_ring.add(account_uuid, "mail.deleted", {
                    "id": msg_id, "account_id": account_id_str, "folder_id": folder_id,
                })

    dsn = parse_dsn_from_sqlalchemy_url(config.database.url)
    _pg_listener = PgListener(dsn)
    _pg_listener.add_handler(_on_pg_event)
    await _pg_listener.start()
    logger.info("PG LISTEN dispatcher started")

    yield

    # Cleanup (reverse order)
    logger.info("MailVerdict server shutting down")

    if _pg_listener:
        await _pg_listener.stop()
        logger.info("PG LISTEN stopped")

    _spam_processor = None
    _rules_engine = None

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

    reset_openai_provider()
    reset_settings_service()
    await close_database()
    logger.info("Database connection closed")

    _qdrant_client = None
    _pg_listener = None


def _build_fastapi() -> FastAPI:
    """Build the FastAPI sub-app with all REST routers and health endpoint."""
    from mail_verdict.api.auth import require_auth

    fastapi_app = FastAPI(
        title="MailVerdict",
        version="2.0.0",
        dependencies=[Depends(require_auth)],
    )

    from mail_verdict.api.routes import all_routers

    for router in all_routers:
        fastapi_app.include_router(router)

    @fastapi_app.get("/health", dependencies=[])
    async def health() -> JSONResponse:
        """Health check endpoint."""
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

        dependencies["postimap"] = "managed externally"

        all_ok = all(v == "ok" for v in dependencies.values() if v != "managed externally")

        return JSONResponse(
            status_code=200 if all_ok else 503,
            content={
                "status": "healthy" if all_ok else "degraded",
                "dependencies": dependencies,
            },
        )

    return fastapi_app


def create_app() -> ASGIApp:
    """Create the MailVerdict ASGI application."""
    config = get_config()
    fastapi_app = _build_fastapi()

    from mail_verdict.api.events import sse_endpoint
    from mail_verdict.api.mcp_tools import mcp as mcp_server

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

    composed_app.routes.insert(0, Mount("/api", app=fastapi_app, name="fastapi"))
    composed_app.routes.insert(0, Route("/api/events", sse_endpoint))
    composed_app.router.lifespan_context = lifespan  # type: ignore[attr-defined]

    ui_build_dir = Path(__file__).parent.parent.parent / "ui" / "build"
    if not ui_build_dir.exists():
        ui_build_dir = Path("/app/ui/build")

    if ui_build_dir.exists():
        next_dir = ui_build_dir / "_next"
        if next_dir.exists():
            composed_app.routes.append(
                Mount("/_next", app=StaticFiles(directory=str(next_dir)), name="next-assets")
            )

        async def spa_fallback(request: Any) -> FileResponse | JSONResponse:
            """Serve pre-rendered pages and SPA fallback."""
            path = request.path_params.get("path", "")
            if path.startswith("api/") or path.startswith("mcp"):
                return JSONResponse(status_code=404, content={"detail": "Not found"})
            if path:
                exact_file = ui_build_dir / path
                if (
                    exact_file.is_file()
                    and exact_file.resolve().is_relative_to(ui_build_dir.resolve())
                ):
                    return FileResponse(str(exact_file))
            if path:
                page_html = ui_build_dir / f"{path}.html"
                if page_html.exists():
                    return FileResponse(str(page_html))
            index = ui_build_dir / "index.html"
            if index.exists():
                return FileResponse(str(index))
            return JSONResponse(status_code=404, content={"detail": "Not found"})

        composed_app.routes.append(Route("/{path:path}", spa_fallback))
        logger.info("Static UI served from %s", ui_build_dir)

    logger.info("MCP enabled at /mcp (transport=%s)", MCP_TRANSPORT)
    return composed_app
