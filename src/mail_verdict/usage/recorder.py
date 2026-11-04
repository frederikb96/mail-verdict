"""
Recording a paid model response's tokens as an llm_usage row.

The calls that reach a provider pass through two places, and both record
here once per response, as it arrives: pipeline/context.py's ModelGateway
(every structured call of every task, each retry attempt its own row,
since each is billed) and embeddings/provider.py (stored messages and
search queries).

A failed write is logged and swallowed: losing one usage row must never
fail the call it describes, which has already been paid for.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from mail_verdict.database.models import LlmUsage
from mail_verdict.usage.tokens import TokenUsage, provider_label

if TYPE_CHECKING:
    from mail_verdict.database.connection import DatabaseConnection

logger = logging.getLogger(__name__)

# The purpose a ModelGateway call is recorded under is its category (the AI
# task whose settings the call came from), except where the category's name
# does not say what the calls are for. Embeddings are not gateway calls and
# name their own purposes.
_RENAMED_CATEGORIES: dict[str, str] = {"ai": "verdict"}
EMBEDDING_PURPOSE = "embedding"
QUERY_EMBEDDING_PURPOSE = "embedding.query"


def gateway_purpose(category: str) -> str:
    """The purpose a ModelGateway call of `category` is recorded under:
    `verdict`, `assistant`, `orders.decide`, `orders.write`, `orders.read`."""
    return _RENAMED_CATEGORIES.get(category, category)


class UsageRecorder:
    """Writes the usage of one purpose's calls."""

    def __init__(self, db: DatabaseConnection, purpose: str) -> None:
        """
        Args:
            db: Database connection
            purpose: What the calls are for, the same for every call this
                recorder sees (gateway_purpose(), EMBEDDING_PURPOSE, ...)
        """
        self._db = db
        self._purpose = purpose

    async def record(
        self, *, provider: str, base_url: str | None, model: str, tokens: TokenUsage,
    ) -> None:
        """
        Write one response's usage. Never raises.

        Args:
            provider: The task's provider setting
            base_url: The compatible server's address, for "custom"
            model: The model the request named
            tokens: The response's token counts
        """
        try:
            async with self._db.session() as session:
                session.add(LlmUsage(
                    purpose=self._purpose,
                    provider=provider_label(provider, base_url),
                    model=model,
                    input_tokens=tokens.input,
                    cache_read_tokens=tokens.cache_read,
                    cache_write_tokens=tokens.cache_write,
                    output_tokens=tokens.output,
                ))
        except Exception:
            logger.exception(
                "Recording model usage failed", extra={"purpose": self._purpose, "model": model},
            )

    def for_call(
        self, *, provider: str, base_url: str | None, model: str,
    ) -> Callable[[TokenUsage], Awaitable[None]]:
        """
        A callback recording each response of one call, for
        core/structured_llm.py's `on_usage`.

        Args:
            provider: The task's provider setting
            base_url: The compatible server's address, for "custom"
            model: The model the request names
        """

        async def _record(tokens: TokenUsage) -> None:
            await self.record(provider=provider, base_url=base_url, model=model, tokens=tokens)

        return _record
