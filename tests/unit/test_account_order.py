"""Tests for the account order schema and router registration."""

from __future__ import annotations

import uuid


class TestAccountOrderSchemas:
    def test_account_order_schemas(self) -> None:
        from mail_verdict.api.schemas import AccountOrderResponse, AccountOrderUpdate

        account_id = uuid.uuid4()

        update = AccountOrderUpdate(order=[account_id])
        assert update.order == [account_id]

        resp = AccountOrderResponse(order=[account_id])
        assert resp.order == [account_id]

    def test_account_order_schemas_allow_empty(self) -> None:
        from mail_verdict.api.schemas import AccountOrderResponse, AccountOrderUpdate

        assert AccountOrderUpdate(order=[]).order == []
        assert AccountOrderResponse(order=[]).order == []


class TestAccountOrderRouterRegistration:
    def test_account_order_router_is_registered(self) -> None:
        from mail_verdict.api.routes import all_routers

        paths = [
            route.path
            for router in all_routers
            for route in router.routes  # type: ignore[union-attr]
        ]
        assert "/account-order" in paths
