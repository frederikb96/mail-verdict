"""
The Pipeline settings page's queue cards: a suspended circuit is shown,
and "Retry now" closes it immediately through the existing reset
endpoint rather than making a person wait out its own probe interval.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import timedelta

from playwright.sync_api import Page, expect

from mail_verdict.config.loader import DatabaseConfig
from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.queue.circuit import CircuitBreaker


async def _suspend_pipeline_circuit(postgres_url: str) -> None:
    """Suspends the breaker `PipelineRunner._circuit_name()` actually
    resolves to for a fresh install (`ai.provider` defaults to "openai",
    category "ai") -- a standalone connection, never the app's own pool,
    so this needs no loop shared with anything app_server owns."""
    connection = DatabaseConnection(
        DatabaseConfig(url=postgres_url, pool_size=1, max_overflow=0, reserved_for_requests=0)
    )
    await connection.init()
    try:
        await CircuitBreaker(connection, "openai:ai").record_unavailable(
            reason="openai rejected the API key", probe_interval=timedelta(minutes=5),
        )
    finally:
        await connection.close()


class TestQueueCardCircuitControl:
    def test_retry_now_closes_a_suspended_circuit(
        self, page: Page, app_server: str, postgres_url: str,
    ) -> None:
        # pytest-playwright's sync API keeps a loop appearing "running" on
        # this thread for the duration of the page fixture -- asyncio.run()
        # refuses to nest inside it, the same reason app_server's own
        # migrations run in a worker thread (tests/ui/conftest.py).
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(asyncio.run, _suspend_pipeline_circuit(postgres_url)).result()

        page.goto(f"{app_server}/pipeline")
        pipeline_card = page.locator('[data-slot="card"]').filter(has_text="pipeline")
        expect(pipeline_card.get_by_text("Circuit suspended", exact=False)).to_be_visible(
            timeout=15_000,
        )
        expect(
            pipeline_card.get_by_text("openai rejected the API key", exact=False)
        ).to_be_visible()

        pipeline_card.get_by_role("button", name="Retry now").click()

        expect(pipeline_card.get_by_text("Circuit suspended", exact=False)).not_to_be_visible(
            timeout=15_000,
        )
        expect(pipeline_card.get_by_role("button", name="Retry now")).not_to_be_visible()
