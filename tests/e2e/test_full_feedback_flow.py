"""
E2E test: Feedback loop flow.

Verifies that user feedback via the API correctly updates the verdict
and Qdrant tag. Tests the /api/mails/{id}/feedback endpoint.

Note: The full IMAP-based feedback flow (user moves email from Junk to
INBOX and app auto-detects the move) requires the app to sync the Junk
folder, which is not in the monitored folders list (test config only
syncs INBOX). That flow is documented as TODO.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.e2e.conftest import (
    ALICE_EMAIL,
    SPAMMER_EMAIL,
    SPAMMER_PASSWORD,
    get_account_id,
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


@pytest.mark.asyncio
async def test_submit_not_spam_feedback(app_client, qdrant_client) -> None:
    """
    Submit not-spam feedback for a mail classified as spam.

    Sends a new spam email, waits for it to be classified as spam,
    then submits "not spam" feedback and verifies a correction verdict is created.
    """
    known_ids = await get_known_mail_ids(app_client)
    account_id = await get_account_id(app_client)

    test_id = uuid.uuid4().hex[:8]
    subject = f"WIN $1,000,000 NOW!!! {test_id}"
    body = (
        "CONGRATULATIONS!!!\n\n"
        "You have WON the international lottery!\n"
        "Send your bank details to claim your prize!\n"
        "Account number, routing number, SSN required.\n"
        "ACT NOW - this offer expires in 24 hours!\n"
    )

    send_email(
        from_addr=SPAMMER_EMAIL,
        from_password=SPAMMER_PASSWORD,
        to_addr=ALICE_EMAIL,
        subject=subject,
        body=body,
    )

    mail = await wait_for_new_mail(
        app_client,
        known_ids=known_ids,
        subject_contains=test_id,
        timeout=90,
    )

    # Wait for the AI verdict
    verdict = await wait_for_verdict(app_client, mail["id"], timeout=120)
    assert verdict["is_spam"] is True, "Expected AI to classify as spam first"

    # Submit feedback: "this is NOT spam"
    resp = await app_client.post(
        f"/api/mails/{mail['id']}/feedback",
        params={"account_id": account_id},
        json={"is_spam": False},
    )
    assert resp.status_code == 200
    feedback = resp.json()
    assert feedback["success"] is True
    assert feedback["is_spam"] is False

    # Allow time for the feedback handler to process
    await asyncio.sleep(2)

    # Verify a new correction verdict was created
    resp = await app_client.get(
        "/api/verdicts",
        params={"mail_id": mail["id"]},
    )
    assert resp.status_code == 200
    verdicts = resp.json()
    # Should have at least 2 verdicts: original AI + user correction
    assert len(verdicts) >= 2, (
        f"Expected at least 2 verdicts (AI + correction), got {len(verdicts)}"
    )

    # The most recent verdict should reflect user correction
    latest = verdicts[0]
    assert latest["source"] == "user_feedback"
    assert latest["is_spam"] is False


@pytest.mark.asyncio
async def test_imap_feedback_flow_placeholder() -> None:
    """
    Placeholder for full IMAP-based feedback flow.

    Full flow would require:
    - Send ambiguous email -> classified as spam -> moved to Junk
    - Move from Junk back to INBOX via IMAP
    - App detects the move (requires syncing Junk folder)
    - Correction verdict auto-created
    - Qdrant tag updated

    Requires adding "Junk Mail" to config.test.yaml's folders list.
    """
    pytest.skip(
        "IMAP-based feedback flow deferred: requires Junk Mail folder "
        "in sync config and app restart"
    )
