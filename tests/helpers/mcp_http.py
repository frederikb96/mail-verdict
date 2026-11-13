"""
Calling MCP tools over HTTP through a Starlette TestClient.

The in-memory `fastmcp.Client(mcp)` skips HTTP entirely, so nothing a request
carries -- its bearer token above all -- reaches the tool. Tests about who a
tool acts for go through the real `/mcp/` endpoint instead: initialize a
session, then call tools on it, each request with whatever headers the test
chooses.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.testclient import TestClient

_ACCEPT = "application/json, text/event-stream"


class McpHttpSession:
    """One MCP session on a TestClient, opened with `headers`."""

    def __init__(self, client: TestClient, path: str, headers: dict[str, str]) -> None:
        self._client = client
        self._path = path
        self._next_id = 1
        response = self._post(
            {
                "jsonrpc": "2.0", "id": self._take_id(), "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            headers,
        )
        assert response.status_code == 200, response.text
        self.session_id = response.headers["mcp-session-id"]
        notified = self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers,
        )
        assert notified.status_code in (200, 202), notified.text

    def call_tool(
        self, name: str, arguments: dict[str, Any], headers: dict[str, str],
    ) -> dict[str, Any]:
        """The JSON-RPC result (or error) of one tools/call, sent with `headers`."""
        response = self._post(
            {
                "jsonrpc": "2.0", "id": self._take_id(), "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers,
        )
        return _json_rpc_message(response.status_code, response.headers, response.text)

    def _take_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _post(self, payload: dict[str, Any], headers: dict[str, str]) -> Any:
        extra = {"accept": _ACCEPT, "content-type": "application/json"}
        if hasattr(self, "session_id"):
            extra["mcp-session-id"] = self.session_id
        return self._client.post(self._path, content=json.dumps(payload), headers={
            **extra, **headers,
        })


def _json_rpc_message(status: int, headers: Any, body: str) -> dict[str, Any]:
    """The JSON-RPC message in a response, whether sent as JSON or as one
    server-sent event."""
    assert status == 200, f"{status}: {body}"
    if headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[len("data:"):].strip() for line in body.splitlines()
                if line.startswith("data:")]
        assert data, body
        message: dict[str, Any] = json.loads(data[-1])
        return message
    parsed: dict[str, Any] = json.loads(body)
    return parsed
