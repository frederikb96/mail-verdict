"""The semantic endpoint answers 503 semantic_unavailable when the query cannot be embedded."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import HTTPException

from mail_verdict.api.embeddings import search
from mail_verdict.embeddings.query import SemanticUnavailableError


@pytest.mark.asyncio
async def test_a_query_that_cannot_be_embedded_is_a_503_with_the_code() -> None:
    async def unavailable(*args: object, **kwargs: object) -> None:
        raise SemanticUnavailableError("rate limited twice")

    with (
        patch("mail_verdict.api.embeddings.search_by_meaning", unavailable),
        patch("mail_verdict.api.embeddings.get_db_connection"),
        pytest.raises(HTTPException) as caught,
    ):
        await search(
            q="garage", account_id=None, folder_ids=None, received_after=None,
            received_before=None,
        )

    assert caught.value.status_code == 503
    assert caught.value.detail == "semantic_unavailable"
