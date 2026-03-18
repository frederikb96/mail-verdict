"""
E2E test: Rules engine flow.

Tests config-driven rules:
- Rule list API reflects config
- Rule test (dry-run) evaluates conditions against real mails
- Full flow: send email matching rule -> rule fires -> tag applied
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.e2e.conftest import (
    ALICE_EMAIL,
    NEWSLETTER_EMAIL,
    NEWSLETTER_PASSWORD,
    get_account_id,
    get_known_mail_ids,
    send_email,
    wait_for_new_mail,
)

pytestmark = [
    pytest.mark.e2e,
]


@pytest.mark.asyncio
async def test_list_rules_returns_configured(app_client) -> None:
    """Config has a newsletter tagging rule."""
    resp = await app_client.get("/api/rules")
    assert resp.status_code == 200
    rules = resp.json()
    assert isinstance(rules, list)
    assert len(rules) >= 1
    assert rules[0]["name"] == "tag-newsletter"
    assert rules[0]["trigger"] == "mail.received"


@pytest.mark.asyncio
async def test_get_rule_by_index(app_client) -> None:
    """Get the newsletter rule by index."""
    resp = await app_client.get("/api/rules/0")
    assert resp.status_code == 200
    rule = resp.json()
    assert rule["name"] == "tag-newsletter"
    assert rule["actions"] == [{"tag": "newsletter"}]


@pytest.mark.asyncio
async def test_get_rule_not_found(app_client) -> None:
    """Non-existent rule index returns 404."""
    resp = await app_client.get("/api/rules/99")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rule_test_dry_run(app_client) -> None:
    """Test the newsletter rule against an existing mail via dry-run."""
    account_id = await get_account_id(app_client)
    resp = await app_client.get("/api/mails", params={"limit": 1})
    mails = resp.json()
    if not mails:
        pytest.skip("No mails available for rule test")

    resp = await app_client.post(
        "/api/rules/0/test",
        json={
            "mail_id": mails[0]["id"],
            "account_id": account_id,
        },
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["rule_name"] == "tag-newsletter"
    assert isinstance(result["conditions_matched"], bool)


@pytest.mark.asyncio
@pytest.mark.slow
async def test_newsletter_rule_tags_email(app_client) -> None:
    """
    Full rules flow: send email from newsletter sender -> rule fires -> tag applied.

    Steps:
    1. Send email from newsletter@test.local
    2. Wait for app to sync the email
    3. Wait for rule engine to process and tag
    4. Verify 'newsletter' tag on the mail via detail API
    """
    known_ids = await get_known_mail_ids(app_client)
    account_id = await get_account_id(app_client)

    test_id = uuid.uuid4().hex[:8]
    subject = f"Monthly Newsletter Update {test_id}"
    body = (
        "Hi there,\n\n"
        "Here's your monthly newsletter with the latest updates.\n\n"
        "- New feature releases\n"
        "- Community highlights\n"
        "- Upcoming events\n\n"
        "Best regards,\n"
        "The Newsletter Team\n"
    )

    send_email(
        from_addr=NEWSLETTER_EMAIL,
        from_password=NEWSLETTER_PASSWORD,
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

    # Wait for rule engine to process and apply tag.
    # The rule fires on mail.received event; allow sync cycles + processing.
    tag_found = False
    detail: dict = {}
    for _ in range(20):
        resp = await app_client.get(
            f"/api/mails/{mail['id']}",
            params={"account_id": account_id},
        )
        if resp.status_code == 200:
            detail = resp.json()
            tags = detail.get("tags", [])
            if any(t["tag_name"] == "newsletter" for t in tags):
                tag_found = True
                break
        await asyncio.sleep(3)

    assert tag_found, (
        f"Expected 'newsletter' tag on mail {mail['id']} but not found after 60s. "
        f"Tags found: {[t['tag_name'] for t in detail.get('tags', [])]}"
    )

    # Verify tag source is 'rule'
    tag = next(t for t in detail["tags"] if t["tag_name"] == "newsletter")
    assert tag["source"] == "rule"
