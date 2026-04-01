"""
E2E test: Rules engine flow.

Tests rules stored in settings DB:
- Rule list API reflects seeded rules
- Rule test (dry-run) evaluates conditions against real mails
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import get_account_id

pytestmark = [pytest.mark.e2e]

NEWSLETTER_RULE = {
    "name": "tag-newsletter",
    "trigger": "mail.received",
    "conditions": {"from_contains": "newsletter@"},
    "actions": [{"tag": "newsletter"}],
    "enrichment": {},
}


async def _ensure_rules(client: httpx.AsyncClient) -> None:
    """Seed rules via settings API if not already present."""
    await client.put(
        "/api/settings/rules",
        json={"data": {"rules": [NEWSLETTER_RULE]}},
    )


@pytest.mark.asyncio
async def test_list_rules_returns_configured(app_client: httpx.AsyncClient) -> None:
    """Seeded rules appear in API."""
    await _ensure_rules(app_client)
    resp = await app_client.get("/api/rules")
    assert resp.status_code == 200
    rules = resp.json()
    assert len(rules) >= 1
    assert rules[0]["name"] == "tag-newsletter"
    assert rules[0]["trigger"] == "mail.received"


@pytest.mark.asyncio
async def test_get_rule_by_index(app_client: httpx.AsyncClient) -> None:
    """Get the newsletter rule by index."""
    await _ensure_rules(app_client)
    resp = await app_client.get("/api/rules/0")
    assert resp.status_code == 200
    rule = resp.json()
    assert rule["name"] == "tag-newsletter"
    assert rule["actions"] == [{"tag": "newsletter"}]


@pytest.mark.asyncio
async def test_get_rule_not_found(app_client: httpx.AsyncClient) -> None:
    """Non-existent rule index returns 404."""
    await _ensure_rules(app_client)
    resp = await app_client.get("/api/rules/99")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rule_test_dry_run(app_client: httpx.AsyncClient) -> None:
    """Test the newsletter rule against an existing mail via dry-run."""
    await _ensure_rules(app_client)
    account_id = await get_account_id(app_client, name="alice")
    resp = await app_client.get("/api/mails", params={"limit": 1, "account_id": account_id})
    data = resp.json()
    mails = data.get("messages", data) if isinstance(data, dict) else data
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
