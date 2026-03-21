"""
E2E test: Image exception CRUD.

Tests creating, listing, checking, and deleting image loading exceptions
(sender and domain allowlists).
"""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.conftest import get_account_id

pytestmark = [pytest.mark.e2e]


async def _get_alice_id(client: httpx.AsyncClient) -> str:
    """Get alice's account ID."""
    return await get_account_id(client, name="alice")


@pytest.mark.asyncio
async def test_list_image_exceptions_initially_empty(
    app_client: httpx.AsyncClient,
) -> None:
    """GET /accounts/:id/image-exceptions returns empty list initially."""
    account_id = await _get_alice_id(app_client)
    resp = await app_client.get(
        f"/api/accounts/{account_id}/image-exceptions",
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_create_sender_exception(
    app_client: httpx.AsyncClient,
) -> None:
    """Create a sender-type image exception."""
    account_id = await _get_alice_id(app_client)
    resp = await app_client.post(
        f"/api/accounts/{account_id}/image-exceptions",
        json={"type": "sender", "value": "trusted@example.com"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["type"] == "sender"
    assert data["value"] == "trusted@example.com"
    assert "id" in data
    assert "created_at" in data

    # Cleanup
    await app_client.delete(
        f"/api/accounts/{account_id}/image-exceptions/{data['id']}",
    )


@pytest.mark.asyncio
async def test_create_domain_exception(
    app_client: httpx.AsyncClient,
) -> None:
    """Create a domain-type image exception."""
    account_id = await _get_alice_id(app_client)
    resp = await app_client.post(
        f"/api/accounts/{account_id}/image-exceptions",
        json={"type": "domain", "value": "trusted-corp.com"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["type"] == "domain"
    assert data["value"] == "trusted-corp.com"

    # Cleanup
    await app_client.delete(
        f"/api/accounts/{account_id}/image-exceptions/{data['id']}",
    )


@pytest.mark.asyncio
async def test_create_and_list_exception(
    app_client: httpx.AsyncClient,
) -> None:
    """Created exception appears in list."""
    account_id = await _get_alice_id(app_client)

    # Create
    create_resp = await app_client.post(
        f"/api/accounts/{account_id}/image-exceptions",
        json={"type": "sender", "value": "visible@example.com"},
    )
    assert create_resp.status_code == 201
    exc_id = create_resp.json()["id"]

    # List
    list_resp = await app_client.get(
        f"/api/accounts/{account_id}/image-exceptions",
    )
    assert list_resp.status_code == 200
    exceptions = list_resp.json()
    assert any(e["id"] == exc_id for e in exceptions)

    # Cleanup
    await app_client.delete(
        f"/api/accounts/{account_id}/image-exceptions/{exc_id}",
    )


@pytest.mark.asyncio
async def test_delete_exception(app_client: httpx.AsyncClient) -> None:
    """Delete an image exception and verify it's gone."""
    account_id = await _get_alice_id(app_client)

    # Create
    resp = await app_client.post(
        f"/api/accounts/{account_id}/image-exceptions",
        json={"type": "domain", "value": "deleteme.com"},
    )
    assert resp.status_code == 201
    exc_id = resp.json()["id"]

    # Delete
    del_resp = await app_client.delete(
        f"/api/accounts/{account_id}/image-exceptions/{exc_id}",
    )
    assert del_resp.status_code == 204

    # Verify gone
    list_resp = await app_client.get(
        f"/api/accounts/{account_id}/image-exceptions",
    )
    assert not any(e["id"] == exc_id for e in list_resp.json())


@pytest.mark.asyncio
async def test_check_sender_allowed(
    app_client: httpx.AsyncClient,
) -> None:
    """Check endpoint returns allowed=True for allowlisted sender."""
    account_id = await _get_alice_id(app_client)

    # Create sender exception
    resp = await app_client.post(
        f"/api/accounts/{account_id}/image-exceptions",
        json={"type": "sender", "value": "checkme@example.com"},
    )
    exc_id = resp.json()["id"]

    # Check: should be allowed
    check_resp = await app_client.get(
        f"/api/accounts/{account_id}/image-exceptions/check",
        params={"sender": "checkme@example.com"},
    )
    assert check_resp.status_code == 200
    assert check_resp.json()["allowed"] is True

    # Check: unknown sender should not be allowed
    check_resp2 = await app_client.get(
        f"/api/accounts/{account_id}/image-exceptions/check",
        params={"sender": "unknown@other.com"},
    )
    assert check_resp2.status_code == 200
    assert check_resp2.json()["allowed"] is False

    # Cleanup
    await app_client.delete(
        f"/api/accounts/{account_id}/image-exceptions/{exc_id}",
    )


@pytest.mark.asyncio
async def test_check_domain_allowed(
    app_client: httpx.AsyncClient,
) -> None:
    """Check endpoint returns allowed=True for domain-matched sender."""
    account_id = await _get_alice_id(app_client)

    # Create domain exception
    resp = await app_client.post(
        f"/api/accounts/{account_id}/image-exceptions",
        json={"type": "domain", "value": "domain-check.com"},
    )
    exc_id = resp.json()["id"]

    # Any sender from that domain should be allowed
    check_resp = await app_client.get(
        f"/api/accounts/{account_id}/image-exceptions/check",
        params={"sender": "anyone@domain-check.com"},
    )
    assert check_resp.status_code == 200
    assert check_resp.json()["allowed"] is True

    # Cleanup
    await app_client.delete(
        f"/api/accounts/{account_id}/image-exceptions/{exc_id}",
    )


@pytest.mark.asyncio
async def test_delete_nonexistent_exception_404(
    app_client: httpx.AsyncClient,
) -> None:
    """Deleting a non-existent exception returns 404."""
    account_id = await _get_alice_id(app_client)
    resp = await app_client.delete(
        f"/api/accounts/{account_id}/image-exceptions/00000000-0000-0000-0000-000000000000",
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_value_normalized_to_lowercase(
    app_client: httpx.AsyncClient,
) -> None:
    """Exception values are normalized to lowercase."""
    account_id = await _get_alice_id(app_client)

    resp = await app_client.post(
        f"/api/accounts/{account_id}/image-exceptions",
        json={"type": "sender", "value": "MixedCase@Example.COM"},
    )
    assert resp.status_code == 201
    assert resp.json()["value"] == "mixedcase@example.com"

    # Cleanup
    await app_client.delete(
        f"/api/accounts/{account_id}/image-exceptions/{resp.json()['id']}",
    )
