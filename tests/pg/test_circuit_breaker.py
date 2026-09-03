"""
CircuitBreaker: closed by default, opens for a bounded rate-limit/error
window, suspends on an auth failure and only clears via a probe.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.queue.circuit import CircuitBreaker, CircuitState


def _name() -> str:
    return f"provider-{uuid.uuid4().hex[:8]}"


class TestDefaultState:
    @pytest.mark.asyncio
    async def test_an_unknown_name_reads_as_closed(self, migrated_db: DatabaseConnection) -> None:
        breaker = CircuitBreaker(migrated_db, _name())

        status = await breaker.status()

        assert status.state == CircuitState.CLOSED
        assert await breaker.is_available() is True


class TestBackoff:
    @pytest.mark.asyncio
    async def test_open_is_unavailable_until_its_cooldown_passes(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        breaker = CircuitBreaker(migrated_db, _name())

        await breaker.record_backoff(retry_after=timedelta(hours=1), reason="429")

        status = await breaker.status()
        assert status.state == CircuitState.OPEN
        assert await breaker.is_available() is False

    @pytest.mark.asyncio
    async def test_open_becomes_available_once_the_cooldown_elapses(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Open self-clears on the passage of time alone -- no probe required,
        unlike suspended."""
        name = _name()
        breaker = CircuitBreaker(migrated_db, name)
        await breaker.record_backoff(retry_after=timedelta(seconds=30), reason="429")
        async with migrated_db.session() as session:
            await session.execute(
                text("UPDATE circuit_breakers SET retry_after = now() - interval '1 second' "
                     "WHERE name = :name"),
                {"name": name},
            )

        assert await breaker.is_available() is True

    @pytest.mark.asyncio
    async def test_record_success_closes_it_immediately(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        breaker = CircuitBreaker(migrated_db, _name())
        await breaker.record_backoff(retry_after=timedelta(hours=1), reason="429")

        await breaker.record_success()

        status = await breaker.status()
        assert status.state == CircuitState.CLOSED
        assert status.reason is None


class TestSuspension:
    @pytest.mark.asyncio
    async def test_unavailable_suspends_and_stays_unavailable_without_a_probe(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """A 401 suspends the breaker -- unlike open, the passage of time alone
        must never make it available again."""
        name = _name()
        breaker = CircuitBreaker(migrated_db, name)

        await breaker.record_unavailable(
            reason="401: invalid api key", probe_interval=timedelta(seconds=60),
        )

        status = await breaker.status()
        assert status.state == CircuitState.SUSPENDED
        assert status.reason == "401: invalid api key"
        assert await breaker.is_available() is False

        async with migrated_db.session() as session:
            await session.execute(
                text("UPDATE circuit_breakers SET retry_after = now() - interval '1 hour' "
                     "WHERE name = :name"),
                {"name": name},
            )
        assert await breaker.is_available() is False

    @pytest.mark.asyncio
    async def test_a_successful_probe_resumes_the_breaker(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The exact recovery sequence: suspend, win the right to probe, the probe
        call succeeds, record_success clears it."""
        breaker = CircuitBreaker(migrated_db, _name())
        await breaker.record_unavailable(
            reason="401", probe_interval=timedelta(seconds=0),
        )
        assert await breaker.is_available() is False

        may_probe = await breaker.try_probe(probe_interval=timedelta(seconds=60))
        assert may_probe is True

        await breaker.record_success()

        assert await breaker.is_available() is True

    @pytest.mark.asyncio
    async def test_only_one_probe_is_granted_per_interval(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """Several idle workers racing a suspended breaker must not all fire a
        probe call at once against still-revoked credentials."""
        breaker = CircuitBreaker(migrated_db, _name())
        await breaker.record_unavailable(reason="401", probe_interval=timedelta(seconds=0))

        first = await breaker.try_probe(probe_interval=timedelta(seconds=60))
        second = await breaker.try_probe(probe_interval=timedelta(seconds=60))

        assert first is True
        assert second is False

    @pytest.mark.asyncio
    async def test_try_probe_is_a_noop_when_not_suspended(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        breaker = CircuitBreaker(migrated_db, _name())

        may_probe = await breaker.try_probe(probe_interval=timedelta(seconds=60))

        assert may_probe is False


class TestSuspensionLogging:
    @pytest.mark.asyncio
    async def test_suspending_logs_rather_than_crashing_the_caller(
        self, migrated_db: DatabaseConnection,
    ) -> None:
        """The suspension log line's `extra` dict must not collide with a
        reserved LogRecord attribute -- `name` does, and the module logger
        must actually be enabled to prove it: alembic's env.py calls
        fileConfig() while migrated_db runs migrations, and
        fileConfig()'s default disable_existing_loggers=True sets
        `.disabled = True` on this module's logger, which otherwise masks
        the crash by skipping the log call entirely."""
        records: list[logging.LogRecord] = []

        class _CollectingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _CollectingHandler()
        circuit_logger = logging.getLogger("mail_verdict.queue.circuit")
        circuit_logger.addHandler(handler)
        was_disabled = circuit_logger.disabled
        circuit_logger.disabled = False
        try:
            breaker = CircuitBreaker(migrated_db, _name())
            await breaker.record_unavailable(
                reason="401: invalid api key", probe_interval=timedelta(seconds=60),
            )
        finally:
            circuit_logger.removeHandler(handler)
            circuit_logger.disabled = was_disabled

        assert any("Circuit breaker suspended" in r.getMessage() for r in records)
