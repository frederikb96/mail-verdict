"""
E2E test: Feedback loop flow.

Tests two feedback mechanisms:
1. API feedback endpoint (/api/mails/{id}/feedback)
2. IMAP folder move: Junk Mail -> INBOX with API correction

The IMAP test verifies the full user workflow:
  send spam -> AI verdict -> app moves to Junk Mail ->
  user moves back to INBOX via IMAP -> submits not-spam feedback ->
  correction verdict created -> Qdrant tag updated
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.e2e.conftest import (
    ALICE_EMAIL,
    ALICE_PASSWORD,
    SPAMMER_EMAIL,
    SPAMMER_PASSWORD,
    get_account_id,
    get_known_mail_ids,
    imap_get_messages,
    imap_move_message,
    send_email,
    wait_for_new_mail,
    wait_for_verdict,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.slow,
    pytest.mark.llm,
]

JUNK_FOLDER = "Junk Mail"


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
async def test_imap_feedback_junk_to_inbox(app_client, qdrant_client) -> None:
    """
    Full IMAP feedback flow: spam classified, moved to Junk, user moves back.

    Steps:
    1. Send obvious spam via SMTP
    2. Wait for AI to classify as spam
    3. Wait for app to move email to Junk Mail folder
    4. Move email from Junk Mail back to INBOX via IMAP
    5. Submit not-spam feedback via API
    6. Verify correction verdict created (source=user_feedback, is_spam=False)
    7. Verify Qdrant embedding tag updated to is_spam=false
    """
    known_ids = await get_known_mail_ids(app_client)
    account_id = await get_account_id(app_client)

    test_id = uuid.uuid4().hex[:8]
    subject = f"CLAIM YOUR FREE PRIZE {test_id}"
    body = (
        "YOU HAVE BEEN SELECTED!!!\n\n"
        "Dear lucky winner,\n"
        "You have won $5,000,000 in the Nigerian National Lottery!\n"
        "To claim your winnings, please provide:\n"
        "- Full name and address\n"
        "- Bank account number and routing number\n"
        "- Social security number\n"
        "- Copy of your passport\n\n"
        "Send all details to: claim-prize@totally-legit.example.com\n"
        "THIS IS NOT A SCAM. ACT NOW BEFORE IT EXPIRES!\n"
    )

    send_email(
        from_addr=SPAMMER_EMAIL,
        from_password=SPAMMER_PASSWORD,
        to_addr=ALICE_EMAIL,
        subject=subject,
        body=body,
    )

    # Wait for the app to sync and classify
    mail = await wait_for_new_mail(
        app_client,
        known_ids=known_ids,
        subject_contains=test_id,
        timeout=90,
    )

    verdict = await wait_for_verdict(app_client, mail["id"], timeout=120)
    assert verdict["is_spam"] is True, "Expected AI to classify as spam"
    assert verdict["source"] == "ai"

    # Wait for the app to move the email to Junk Mail.
    # The pipeline moves spam after verdict creation; allow sync cycles.
    junk_found = False
    for _ in range(30):
        junk_msgs = imap_get_messages(
            user=ALICE_EMAIL,
            password=ALICE_PASSWORD,
            folder=JUNK_FOLDER,
        )
        if any(test_id in (m.get("subject") or "") for m in junk_msgs):
            junk_found = True
            break
        await asyncio.sleep(3)

    assert junk_found, (
        f"Email with '{test_id}' not found in {JUNK_FOLDER} after 90s. "
        "The spam pipeline may have failed to move it."
    )

    # Get the UID of the email in Junk Mail
    junk_msgs = imap_get_messages(
        user=ALICE_EMAIL,
        password=ALICE_PASSWORD,
        folder=JUNK_FOLDER,
    )
    target_msg = next(m for m in junk_msgs if test_id in (m.get("subject") or ""))
    junk_uid = target_msg["uid"]
    assert junk_uid is not None, "Could not determine UID in Junk Mail"

    # Move email from Junk Mail back to INBOX via IMAP (simulates user action)
    moved = imap_move_message(
        uid=junk_uid,
        from_folder=JUNK_FOLDER,
        to_folder="INBOX",
        user=ALICE_EMAIL,
        password=ALICE_PASSWORD,
    )
    assert moved, "IMAP MOVE from Junk Mail to INBOX failed"

    # Verify email is back in INBOX via IMAP
    inbox_msgs = imap_get_messages(
        user=ALICE_EMAIL,
        password=ALICE_PASSWORD,
        folder="INBOX",
    )
    assert any(test_id in (m.get("subject") or "") for m in inbox_msgs), (
        "Email not found in INBOX after IMAP move"
    )

    # Verify email is gone from Junk Mail
    junk_after = imap_get_messages(
        user=ALICE_EMAIL,
        password=ALICE_PASSWORD,
        folder=JUNK_FOLDER,
    )
    assert not any(test_id in (m.get("subject") or "") for m in junk_after), (
        "Email still in Junk Mail after IMAP move"
    )

    # Submit not-spam feedback via API (the working correction mechanism)
    resp = await app_client.post(
        f"/api/mails/{mail['id']}/feedback",
        params={"account_id": account_id},
        json={"is_spam": False},
    )
    assert resp.status_code == 200
    feedback = resp.json()
    assert feedback["success"] is True

    # Allow processing time
    await asyncio.sleep(2)

    # Verify correction verdict was created
    resp = await app_client.get(
        "/api/verdicts",
        params={"mail_id": mail["id"]},
    )
    assert resp.status_code == 200
    verdicts = resp.json()
    assert len(verdicts) >= 2, (
        f"Expected at least 2 verdicts (AI + correction), got {len(verdicts)}"
    )

    latest = verdicts[0]
    assert latest["source"] == "user_feedback"
    assert latest["is_spam"] is False

    # Verify Qdrant tag was updated to not-spam
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
    assert len(points) >= 1, "No Qdrant point found for mail"

    payload = points[0]["payload"]
    assert payload.get("is_spam") == "false", (
        f"Expected is_spam=false in Qdrant after feedback, got {payload.get('is_spam')}"
    )
