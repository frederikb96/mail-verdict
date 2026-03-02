"""
E2E test: Rules engine flow.

Rules in MailVerdict are config-driven (defined in config.yaml, not via API).
The test config has an empty rules list, so runtime rule CRUD is not possible.

These tests verify:
1. Rules API returns empty list (matching config)
2. Rule test (dry-run) endpoint works against existing mails

TODO: Full rules flow test requires config.test.yaml to include a rule
definition, plus app restart to pick it up. This is deferred to when
runtime rule management is implemented.
"""

from __future__ import annotations

import pytest

from tests.e2e.conftest import get_account_id

pytestmark = [
    pytest.mark.e2e,
]


@pytest.mark.asyncio
async def test_list_rules_returns_empty(app_client) -> None:
    """Config has no rules, so list should return empty."""
    resp = await app_client.get("/api/rules")
    assert resp.status_code == 200
    rules = resp.json()
    assert isinstance(rules, list)
    assert len(rules) == 0


@pytest.mark.asyncio
async def test_get_rule_not_found(app_client) -> None:
    """Non-existent rule index returns 404."""
    resp = await app_client.get("/api/rules/0")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_test_rule_not_found(app_client) -> None:
    """Test endpoint for non-existent rule returns 404."""
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
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rules_flow_placeholder() -> None:
    """
    Placeholder: full rules flow requires runtime rule management.

    Full flow would require:
    - Add rule to config.test.yaml (e.g., sender contains 'newsletter' -> tag 'newsletter')
    - Restart app container to reload config
    - Send matching email
    - Wait for sync + rule execution
    - Verify tag applied via API

    Deferred until runtime rule management (create/update/delete via API) is implemented.
    """
    pytest.skip(
        "Full rules flow test deferred: rules are config-driven, "
        "runtime CRUD not yet implemented"
    )
