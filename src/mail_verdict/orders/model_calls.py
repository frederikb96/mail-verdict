"""
The orders queue's two model tasks, each resolved from its own settings.

`decide` (which order does a mail belong to, or none) and `write` (an
order's text) each name their own provider, server address, model,
reasoning effort and token budget in the `orders` settings category, as
`decide_*` and `write_*` keys. Every call reads them fresh, so a change
takes effect on the next job. Both go through the one ModelGateway, under
the breaker categories `orders.decide` and `orders.write`: a provider
failing one task never stalls the other when they sit on different
providers, and a bad model name in one task trips only that task's breaker.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from mail_verdict.core.retry import RetryConfig
from mail_verdict.pipeline.context import ModelGateway
from mail_verdict.pipeline.contracts import StageThrottled, StageUnavailable
from mail_verdict.queue.circuit import CircuitBreaker, CircuitState

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection
    from mail_verdict.settings.credentials import ProviderCredentialRepository
    from mail_verdict.settings.service import SettingsService

Task = Literal["decide", "write"]


@dataclass(frozen=True)
class TaskModel:
    """One task's model as the settings name it right now."""

    task: Task
    provider: str
    base_url: str | None
    model: str
    effort: str | None
    max_tokens: int
    timeout_seconds: float

    @property
    def category(self) -> str:
        """The ModelGateway category, and so the breaker, of this task."""
        return f"orders.{self.task}"

    @property
    def circuit_name(self) -> str:
        """The name of this task's breaker, as ModelGateway keys it."""
        return f"{self.provider}:{self.category}"

    @classmethod
    def from_settings(cls, settings_service: SettingsService, task: Task) -> TaskModel:
        settings = settings_service.get("orders")
        return cls(
            task=task,
            provider=str(settings[f"{task}_provider"]).lower(),
            base_url=settings[f"{task}_base_url"] or None,
            model=str(settings[f"{task}_model"]),
            effort=settings[f"{task}_reasoning_effort"] or None,
            max_tokens=int(settings[f"{task}_max_tokens"]),
            timeout_seconds=float(settings["call_timeout_seconds"]),
        )


def is_fake(settings_service: SettingsService, task: Task) -> bool:
    """Whether this task runs on the keyword-only `fake` provider. Checked
    per task, so a test can fake one and reach a real provider with the
    other."""
    return TaskModel.from_settings(settings_service, task).provider == "fake"


def write_circuit_name(settings_service: SettingsService) -> str:
    """The breaker the orders queue reports: the write task's, the one
    whose suspension leaves orders without text."""
    return TaskModel.from_settings(settings_service, "write").circuit_name


async def ensure_available(
    db: DatabaseConnection, settings_service: SettingsService, task: Task,
) -> None:
    """
    Refuse work for `task` while its breaker is not letting calls through,
    before the job spends anything on reading attachments the call would
    then never receive. A suspended breaker whose probe interval has passed
    is let through: the call itself is the probe.

    Raises:
        StageUnavailable: the task's provider is suspended
        StageThrottled: the task's provider is rate limiting
    """
    model = TaskModel.from_settings(settings_service, task)
    if model.provider == "fake":
        return
    status = await CircuitBreaker(db, model.circuit_name).status()
    if status.state is CircuitState.CLOSED:
        return
    now = datetime.now(timezone.utc)
    if status.retry_after is not None and status.retry_after <= now:
        return
    if status.state is CircuitState.SUSPENDED:
        raise StageUnavailable(f"{model.provider} circuit suspended: {status.reason}")
    raise StageThrottled(
        f"{model.provider} circuit open: {status.reason}",
        retry_after=status.retry_after - now if status.retry_after is not None else None,
    )


async def structured_call(
    db: DatabaseConnection,
    cred_repo: ProviderCredentialRepository,
    settings_service: SettingsService,
    task: Task,
    *, schema_name: str, system_prompt: str, user_prompt: str, schema: dict[str, Any],
    validate: Any,
) -> tuple[dict[str, Any], str, float]:
    """One call of `task` through the shared gateway.

    Returns:
        (parsed response, model name, latency in milliseconds)
    """
    model = TaskModel.from_settings(settings_service, task)
    gateway = ModelGateway(
        db, cred_repo, RetryConfig.from_settings(settings_service.get("retry")),
    )
    data, latency_ms = await gateway.structured_call(
        provider=model.provider, category=model.category, model=model.model,
        effort=model.effort, max_tokens=model.max_tokens, schema_name=schema_name,
        system_prompt=system_prompt, user_prompt=user_prompt, schema=schema,
        validate=validate, base_url=model.base_url, timeout_seconds=model.timeout_seconds,
    )
    return data, model.model, latency_ms
