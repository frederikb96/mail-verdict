"""
E2E test: Full spam detection pipeline.

Verifies the complete flow:
1. Send a clearly spam email via SMTP
2. App syncs the email from IMAP
3. LLM classifies it as spam
4. Verdict is stored in Postgres
5. Embedding exists in Qdrant with is_spam tag
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.e2e.conftest import (
    ALICE_EMAIL,
    NEWSLETTER_EMAIL,
    SPAMMER_EMAIL,
    SPAMMER_PASSWORD,
    get_known_mail_ids,
    send_email,
    wait_for_new_mail,
    wait_for_verdict,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.slow,
    pytest.mark.llm,
]

# Module-level state: share the spam mail across tests to avoid
# sending duplicate emails and making duplicate LLM calls
_spam_mail_cache: dict | None = None
_ham_mail_cache: dict | None = None


@pytest.mark.asyncio
async def test_spam_email_synced_with_correct_metadata(
    app_client: httpx.AsyncClient,
    seeded_env: dict[str, str],
) -> None:
    """Send a spam email and verify it was synced with correct metadata."""
    global _spam_mail_cache

    if _spam_mail_cache is None:
        account_id = seeded_env["account_id"]
        known_ids = await get_known_mail_ids(app_client, account_id=account_id)

        test_id = uuid.uuid4().hex[:8]
        subject = f"URGENT: Buy Cheap Pills NOW!!! {test_id}"
        body = (
            "Dear Friend,\n\n"
            "CONGRATULATIONS! You have been selected for an EXCLUSIVE discount!\n\n"
            "Buy CHEAP VIAGRA and CIALIS at 90% OFF!!!\n"
            "Click here NOW: http://totally-not-a-scam.example.com/pills\n\n"
            "LIMITED TIME OFFER - Act NOW or miss out!\n"
            "Unsubscribe: send $1000 to this account\n\n"
            "This is definitely not spam. Trust us.\n"
        )

        send_email(
            from_addr=SPAMMER_EMAIL,
            from_password=SPAMMER_PASSWORD,
            to_addr=ALICE_EMAIL,
            subject=subject,
            body=body,
        )

        _spam_mail_cache = await wait_for_new_mail(
            app_client,
            known_ids=known_ids,
            subject_contains=test_id,
            account_id=account_id,
            timeout=90,
        )

    mail = _spam_mail_cache
    assert mail["subject"] is not None
    assert "Buy Cheap Pills" in mail["subject"]
    assert mail["from_addr"] == SPAMMER_EMAIL
    assert mail["to_addrs"] is not None

    to_addrs = mail["to_addrs"]
    if isinstance(to_addrs, dict):
        addrs = to_addrs.get("addrs", [])
        assert ALICE_EMAIL in addrs
    elif isinstance(to_addrs, list):
        assert ALICE_EMAIL in to_addrs


@pytest.mark.asyncio
async def test_spam_verdict_is_spam(
    app_client: httpx.AsyncClient,
    seeded_env: dict[str, str],
) -> None:
    """Verify the LLM classified the obvious spam correctly."""
    global _spam_mail_cache

    # Ensure spam mail was sent (run previous test first)
    if _spam_mail_cache is None:
        await test_spam_email_synced_with_correct_metadata(app_client, seeded_env)

    mail = _spam_mail_cache
    assert mail is not None

    verdict = await wait_for_verdict(
        app_client,
        mail["id"],
        timeout=120,
    )

    assert verdict is not None, "No verdict was generated"
    assert verdict["is_spam"] is True, (
        f"Expected spam verdict but got not-spam. "
        f"Model: {verdict.get('model_used')}"
    )
    assert verdict["source"] == "ai"
    assert verdict["model_used"] is not None


@pytest.mark.asyncio
async def test_spam_embedding_exists_in_qdrant(
    app_client: httpx.AsyncClient,
    qdrant_client: httpx.AsyncClient,
    seeded_env: dict[str, str],
) -> None:
    """Verify the email was embedded in Qdrant with spam tag."""
    global _spam_mail_cache

    if _spam_mail_cache is None:
        await test_spam_email_synced_with_correct_metadata(app_client, seeded_env)

    mail = _spam_mail_cache
    assert mail is not None

    # Wait for verdict (embedding happens before verdict in the pipeline)
    await wait_for_verdict(app_client, mail["id"], timeout=120)

    resp = await qdrant_client.post(
        "/collections/mail_embeddings/points/scroll",
        json={
            "filter": {
                "must": [
                    {
                        "key": "mail_id",
                        "match": {"value": mail["id"]},
                    }
                ]
            },
            "limit": 1,
            "with_payload": True,
            "with_vector": False,
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    points = data["result"]["points"]
    assert len(points) == 1, f"Expected 1 Qdrant point for mail, got {len(points)}"

    payload = points[0]["payload"]
    assert payload["mail_id"] == mail["id"]
    assert payload.get("is_spam") == "true", (
        f"Expected is_spam=true in Qdrant payload, got {payload.get('is_spam')}"
    )


@pytest.mark.asyncio
async def test_spam_verdict_in_verdicts_list(
    app_client: httpx.AsyncClient,
    seeded_env: dict[str, str],
) -> None:
    """Verify the verdict is accessible via the verdicts list endpoint."""
    global _spam_mail_cache

    if _spam_mail_cache is None:
        await test_spam_email_synced_with_correct_metadata(app_client, seeded_env)

    mail = _spam_mail_cache
    assert mail is not None

    await wait_for_verdict(app_client, mail["id"], timeout=120)

    resp = await app_client.get(
        "/api/verdicts",
        params={"mail_id": mail["id"]},
    )
    assert resp.status_code == 200
    verdicts = resp.json()
    assert len(verdicts) >= 1
    assert verdicts[0]["mail_id"] == mail["id"]
    assert verdicts[0]["is_spam"] is True


@pytest.mark.asyncio
async def test_legitimate_email_not_spam(
    app_client: httpx.AsyncClient,
    seeded_env: dict[str, str],
) -> None:
    """A clearly legitimate email should be classified as not-spam."""
    global _ham_mail_cache

    if _ham_mail_cache is None:
        account_id = seeded_env["account_id"]
        known_ids = await get_known_mail_ids(app_client, account_id=account_id)

        test_id = uuid.uuid4().hex[:8]
        subject = f"Team meeting agenda for Monday {test_id}"
        body = (
            "Hi Alice,\n\n"
            "Here's the agenda for our Monday team meeting:\n\n"
            "- Sprint retrospective\n"
            "- Q2 planning discussion\n"
            "- Infrastructure migration update\n\n"
            "Please review the attached document before the meeting.\n\n"
            "Best regards,\n"
            "Newsletter Sender\n"
        )

        send_email(
            from_addr=NEWSLETTER_EMAIL,
            from_password=SPAMMER_PASSWORD,
            to_addr=ALICE_EMAIL,
            subject=subject,
            body=body,
        )

        _ham_mail_cache = await wait_for_new_mail(
            app_client,
            known_ids=known_ids,
            subject_contains=test_id,
            account_id=account_id,
            timeout=90,
        )

    mail = _ham_mail_cache
    assert mail is not None

    verdict = await wait_for_verdict(
        app_client,
        mail["id"],
        timeout=120,
    )

    assert verdict is not None, "No verdict was generated for legitimate email"
    assert verdict["is_spam"] is False, (
        f"Legitimate email was incorrectly classified as spam. "
        f"Subject: {mail.get('subject')}"
    )
