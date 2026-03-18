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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route
from starlette.types import ASGIApp

from mail_verdict.config import get_config
from mail_verdict.database import close_database, get_db_connection, init_database
from mail_verdict.rules.bus import EventBus
from mail_verdict.rules.engine import RulesEngine
from mail_verdict.rules.enrichment import EnrichmentRunner
from mail_verdict.rules.executor import ActionExecutor
from mail_verdict.semantic.store import SemanticStore
from mail_verdict.semantic.worker import (
    init_embedding_worker,
    reset_embedding_worker,
)
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
    logger.info("MailVerdict server starting")

    # Initialize database
    await init_database(config.database)
    logger.info("Database initialized")

    # Initialize shared OpenAI client (used by SemanticStore, SpamAnalyst, EnrichmentRunner)
    from openai import AsyncOpenAI

    openai_client = AsyncOpenAI()
    try:
        await openai_client.models.list()
        logger.info("OpenAI client validated")
    except Exception as e:
        logger.warning("OpenAI key validation failed (AI features may not work): %s", e)

    # Initialize Qdrant client
    from qdrant_client import AsyncQdrantClient

    _qdrant_client = AsyncQdrantClient(
        host=config.qdrant.host,
        port=config.qdrant.port,
    )
    logger.info("Qdrant client created")

    # Initialize SemanticStore singleton (uses shared Qdrant + OpenAI clients)
    store = SemanticStore.init_instance(
        _qdrant_client,
        config.qdrant,
        config.ai,
        openai_client,
    )
    await store.ensure_collection()
    logger.info("SemanticStore initialized")

    # Initialize and start EmbeddingWorker
    worker = init_embedding_worker()
    await worker.start()
    logger.info("EmbeddingWorker started")

    # Initialize event bus early so sync engine can emit to it
    global _event_bus, _rules_engine
    _event_bus = EventBus()

    # Start IMAP sync engine (receives event_bus for bridging to rules/SSE)
    global _sync_engine
    db = get_db_connection()
    _sync_engine = SyncEngine(config, db, event_bus=_event_bus)
    await _sync_engine.start()
    logger.info("Sync engine started")

    # Initialize and start spam processing pipeline
    global _spam_processor
    if config.spam.enabled:
        from mail_verdict.database.repository import (
            FolderRepository,
            MailRepository,
            VerdictRepository,
        )
        from mail_verdict.spam.analyst import OpenAISpamAnalyst
        from mail_verdict.spam.feedback import SpamFeedbackHandler
        from mail_verdict.spam.pipeline import VerdictPipeline

        analyst = OpenAISpamAnalyst(config.ai, config.spam, config.retry, openai_client)
        verdict_repo = VerdictRepository(db)
        mail_repo = MailRepository(db)
        folder_repo = FolderRepository(db)

        # Build event queues from sync engine accounts
        event_queues = {}
        for name, account_sync in _sync_engine._accounts.items():
            event_queues[name] = account_sync.manager.event_queue

        feedback = SpamFeedbackHandler(store, verdict_repo)

        if event_queues:
            # Use the first account's action propagator for spam MOVE operations
            first_account_sync = next(iter(_sync_engine._accounts.values()), None)
            action_propagator = first_account_sync.action_propagator if first_account_sync else None

            first_pipeline = VerdictPipeline(
                config=config,
                semantic_store=store,
                analyst=analyst,
                verdict_repo=verdict_repo,
                mail_repo=mail_repo,
                action_propagator=action_propagator,
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
    if config.rules:
        from mail_verdict.database.repository import FolderRepository as FR
        from mail_verdict.database.repository import TagRepository

        tag_repo = TagRepository(db)
        rules_folder_repo = FR(db)
        enrichment_runner = EnrichmentRunner(
            ai_provider=config.ai.provider,
            ai_model=config.ai.model,
            openai_client=openai_client,
        )

        # Build multi-account propagator map for the action executor
        account_propagators: dict[str, ActionPropagator] = {}
        for name, account_sync in _sync_engine._accounts.items():
            account_propagators[name] = account_sync.action_propagator

        # Use the first account's propagator as default (rules engine
        # typically operates on the account that triggered the event)
        default_propagator = next(iter(account_propagators.values()), None)

        action_executor = ActionExecutor(
            propagator=default_propagator,
            tag_repo=tag_repo,
            folder_repo=rules_folder_repo,
        )
        _rules_engine = RulesEngine(
            rules=config.rules,
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

    await close_database()
    logger.info("Database connection closed")

    _qdrant_client = None
    _sync_engine = None
    _spam_processor = None
    _event_bus = None
    _rules_engine = None


def _build_fastapi() -> FastAPI:
    """Build the FastAPI sub-app with all REST routers and health endpoint."""
    fastapi_app = FastAPI(
        title="MailVerdict",
        version="0.1.0",
    )

    from mail_verdict.api.routes import all_routers

    for router in all_routers:
        fastapi_app.include_router(router)

    @fastapi_app.get("/api/health")
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

    if config.mcp.enabled:
        from mail_verdict.api.mcp_tools import mcp as mcp_server

        # MCP creates the base Starlette app
        composed_app = mcp_server.http_app(
            path="/mcp",
            transport=config.mcp.transport,  # type: ignore[arg-type]
        )

        composed_app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.cors_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["*"],
        )

        # Insert SSE route before MCP catch-all
        composed_app.routes.insert(0, Route("/api/events", sse_endpoint))

        # Insert all FastAPI routes at position 0 (priority over MCP)
        for route in reversed(fastapi_app.routes):
            composed_app.routes.insert(0, route)

        # Assign lifespan to the composed app
        composed_app.router.lifespan_context = lifespan  # type: ignore[attr-defined]

        logger.info("MCP enabled at /mcp (transport=%s)", config.mcp.transport)
        return composed_app

    else:
        # Pure FastAPI mode
        fastapi_app.router.lifespan_context = lifespan  # type: ignore[attr-defined]
        fastapi_app.routes.insert(0, Route("/api/events", sse_endpoint))

        fastapi_app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.cors_origins,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["*"],
        )

        return fastapi_app
