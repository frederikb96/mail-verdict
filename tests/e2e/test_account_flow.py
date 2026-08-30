"""
Account onboarding against a real Dovecot mailbox: creation reaches
`active`, the discovered folders and pre-existing mail show up, and --
the assertion that matters most -- syncing years of history into a fresh
account never sends a single message to the spam pipeline.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from starlette.testclient import TestClient

from mail_verdict.database.connection import DatabaseConnection
from mail_verdict.database.models import Verdict
from tests.e2e.helpers import unique_email, wait_for_account_active, wait_for_folder
from tests.setup.containers import DOVECOT_ALIAS, DOVECOT_IMAP_PORT, DOVECOT_PASSWORD
from tests.setup.mail_delivery import build_eml, deliver_message

SEEDED_SUBJECTS = [
    "Quarterly report attached",
    "WIN A FREE LOTTERY PRIZE NOW",
    "Re: lunch tomorrow?",
]


@pytest.fixture(scope="class")
def onboarded_account(
    app_client: TestClient, dovecot_endpoint: tuple[str, int, int],
) -> dict[str, object]:
    """
    One account whose mailbox already held mail before the account existed
    -- the realistic "add an account to an established mailbox" case, and
    exactly the shape backfill suppression exists for. Shared by every
    test in this class so the ~10s sync-to-active wait happens once.
    """
    host, _imap_port, lmtp_port = dovecot_endpoint
    email = unique_email("onboard")

    for subject in SEEDED_SUBJECTS:
        message = build_eml(
            sender="sender@example.com", recipient=email, subject=subject,
            message_id=f"<{uuid.uuid4()}@example.com>",
        )
        deliver_message(message, host, lmtp_port, sender="sender@example.com", recipient=email)

    resp = app_client.post(
        "/api/accounts",
        json={
            "name": email,
            "imap_host": DOVECOT_ALIAS,
            "imap_port": DOVECOT_IMAP_PORT,
            "imap_user": email,
            "imap_password": DOVECOT_PASSWORD,
            "spam_enabled": True,
        },
    )
    assert resp.status_code == 201, resp.text
    account = resp.json()

    wait_for_account_active(app_client, account["id"])
    return account


class TestAccountOnboarding:
    """Several assertions about one onboarded account, sharing its setup cost."""

    def test_inbox_folder_is_discovered_with_the_right_message_count(
        self, app_client: TestClient, onboarded_account: dict[str, object],
    ) -> None:
        """The INBOX folder appears with its total_count matching what was seeded."""
        folder = wait_for_folder(app_client, str(onboarded_account["id"]), "INBOX")
        assert folder["total_count"] == len(SEEDED_SUBJECTS)

    def test_special_use_folders_are_discovered_with_the_right_role(
        self, app_client: TestClient, onboarded_account: dict[str, object],
    ) -> None:
        """
        This throwaway mail world ships Drafts/Junk/Sent/Trash on every
        fresh mailbox with real IMAP SPECIAL-USE attributes -- PostIMAP
        reports them as such on `folders.special_use` with no override
        needed, and the API surfaces the same value.
        """
        resp = app_client.get(f"/api/accounts/{onboarded_account['id']}/folders")
        assert resp.status_code == 200, resp.text
        special_use_by_name = {f["imap_name"]: f["special_use"] for f in resp.json()}
        assert special_use_by_name == {
            "INBOX": "inbox", "Drafts": "drafts", "Junk": "junk",
            "Sent": "sent", "Trash": "trash",
        }

    def test_seeded_messages_appear_after_sync(
        self, app_client: TestClient, onboarded_account: dict[str, object],
    ) -> None:
        """Mail that existed before the account was ever created still syncs in."""
        folder = wait_for_folder(app_client, str(onboarded_account["id"]), "INBOX")
        resp = app_client.get(
            f"/api/accounts/{onboarded_account['id']}/messages",
            params={"folder_id": folder["id"]},
        )
        assert resp.status_code == 200, resp.text
        subjects = {m["subject"] for m in resp.json()["messages"]}
        assert subjects == set(SEEDED_SUBJECTS)

    @pytest.mark.asyncio
    async def test_backfill_produces_zero_verdicts(
        self,
        app_client: TestClient,
        onboarded_account: dict[str, object],
        db: DatabaseConnection,
    ) -> None:
        """
        The single most valuable assertion in the project: syncing a
        mailbox's pre-existing history into a fresh account must never
        treat that history as incoming mail. spam_enabled=True on this
        account and one seeded subject (a blatant lottery-prize spam
        line) both make the assertion meaningful -- if suppression ever
        broke, this is exactly the mail that would produce a verdict.

        Suppression happens at the postimap_events level (no per-row
        `message`/insert during a folder's first full sync, a single
        `sync_complete` instead) -- proven here by the account already
        being `active` (its INBOX backfill is complete) with zero rows in
        `verdicts` for it.
        """
        wait_for_folder(app_client, str(onboarded_account["id"]), "INBOX")
        account_id = uuid.UUID(str(onboarded_account["id"]))

        async with db.session() as session:
            count = await session.scalar(
                select(func.count()).select_from(Verdict).where(Verdict.account_id == account_id)
            )
        assert count == 0
