"""Unit tests for the queue lifecycle API, against a mocked QueueManager."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mail_verdict.api.queues import router
from mail_verdict.queue.circuit import CircuitState, CircuitStatus
from mail_verdict.queue.manager import QueueSummary


def _summary(**overrides: object) -> QueueSummary:
    base: dict[str, object] = {
        "name": "pipeline",
        "state": "running",
        "concurrency_target": 4,
        "concurrency_actual": 4,
        "max_allowed_concurrency": 15,
        "depth": {"pending": 3, "claimed": 1, "done": 100},
        "circuit": CircuitStatus(
            name="pipeline", state=CircuitState.CLOSED, reason=None, since=None, retry_after=None,
        ),
    }
    base.update(overrides)
    return QueueSummary(**base)  # type: ignore[arg-type]


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestListQueues:
    def test_lists_every_registered_queue(self, client: TestClient) -> None:
        manager = AsyncMock()
        manager.list_summaries = AsyncMock(return_value=[_summary()])
        with patch("mail_verdict.api.queues.get_queue_manager", return_value=manager):
            resp = client.get("/queues")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["name"] == "pipeline"
        assert body[0]["concurrency"] == {"target": 4, "actual": 4, "max_allowed": 15}


class TestGetQueue:
    def test_known_name_returns_its_state(self, client: TestClient) -> None:
        manager = AsyncMock()
        manager.summary = AsyncMock(return_value=_summary())
        with patch("mail_verdict.api.queues.get_queue_manager", return_value=manager):
            resp = client.get("/queues/pipeline")
        assert resp.status_code == 200
        assert resp.json()["depth"] == {"pending": 3, "claimed": 1, "done": 100}

    def test_unknown_name_is_404(self, client: TestClient) -> None:
        manager = AsyncMock()
        manager.summary = AsyncMock(side_effect=KeyError("nope"))
        with patch("mail_verdict.api.queues.get_queue_manager", return_value=manager):
            resp = client.get("/queues/nope")
        assert resp.status_code == 404


class TestPatchQueue:
    def test_pause_is_forwarded_to_the_manager(self, client: TestClient) -> None:
        manager = AsyncMock()
        manager.set_state = AsyncMock(return_value=_summary(state="paused"))
        with patch("mail_verdict.api.queues.get_queue_manager", return_value=manager):
            resp = client.patch("/queues/pipeline", json={"state": "paused"})
        assert resp.status_code == 200
        assert resp.json()["state"] == "paused"
        manager.set_state.assert_awaited_once_with(
            "pipeline", state="paused", concurrency=None,
        )

    def test_concurrency_over_pool_capacity_is_400(self, client: TestClient) -> None:
        manager = AsyncMock()
        manager.set_state = AsyncMock(side_effect=ValueError("concurrency 999 exceeds pool"))
        with patch("mail_verdict.api.queues.get_queue_manager", return_value=manager):
            resp = client.patch("/queues/pipeline", json={"concurrency": 999})
        assert resp.status_code == 400
        assert "exceeds pool" in resp.json()["detail"]

    def test_unknown_name_is_404(self, client: TestClient) -> None:
        manager = AsyncMock()
        manager.set_state = AsyncMock(side_effect=KeyError("nope"))
        with patch("mail_verdict.api.queues.get_queue_manager", return_value=manager):
            resp = client.patch("/queues/nope", json={"state": "paused"})
        assert resp.status_code == 404

    def test_invalid_body_is_422(self, client: TestClient) -> None:
        with patch("mail_verdict.api.queues.get_queue_manager", return_value=AsyncMock()):
            resp = client.patch("/queues/pipeline", json={"state": "sleeping"})
        assert resp.status_code == 422

    def test_reset_circuit_is_forwarded_to_the_manager(self, client: TestClient) -> None:
        manager = AsyncMock()
        manager.reset_circuit = AsyncMock()
        manager.set_state = AsyncMock(return_value=_summary())
        with patch("mail_verdict.api.queues.get_queue_manager", return_value=manager):
            resp = client.patch("/queues/pipeline", json={"reset_circuit": True})
        assert resp.status_code == 200
        manager.reset_circuit.assert_awaited_once_with("pipeline")

    def test_omitting_reset_circuit_never_calls_it(self, client: TestClient) -> None:
        manager = AsyncMock()
        manager.reset_circuit = AsyncMock()
        manager.set_state = AsyncMock(return_value=_summary())
        with patch("mail_verdict.api.queues.get_queue_manager", return_value=manager):
            resp = client.patch("/queues/pipeline", json={"state": "paused"})
        assert resp.status_code == 200
        manager.reset_circuit.assert_not_awaited()

    def test_reset_circuit_of_an_unknown_name_is_404(self, client: TestClient) -> None:
        manager = AsyncMock()
        manager.reset_circuit = AsyncMock(side_effect=KeyError("nope"))
        with patch("mail_verdict.api.queues.get_queue_manager", return_value=manager):
            resp = client.patch("/queues/nope", json={"reset_circuit": True})
        assert resp.status_code == 404
