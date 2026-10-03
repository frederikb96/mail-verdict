"""
The webhook action's shape and its validation, in one place.

A rule's `webhook` effect names a destination and how to call it; the body
is always the message's raw RFC 822 source, unmodified, sent as
`message/rfc822`. `body` exists as a field so a later body kind (a
templated JSON body, say) is an addition rather than a change of shape; it
accepts only `raw_message` today.

Header values may reference a stored secret as `{{secret:NAME}}`. The
reference is what a rule, a delivery row and every API response carries;
the value is substituted when the request is made (worker.py) and
nowhere else. A header whose name suggests a credential must carry such a
reference, so a token is never typed into a rule where the pipeline API
would hand it back.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SECRET_REFERENCE = re.compile(r"\{\{secret:([A-Za-z][A-Za-z0-9_]{0,63})\}\}")
SECRET_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
WEBHOOK_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_QUERY_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_CREDENTIAL_HEADER = re.compile(r"authorization|token|key|secret|cookie|password", re.IGNORECASE)
# Set by the delivery itself; a rule cannot override them.
_RESERVED_HEADERS = frozenset({"content-type", "content-length", "host", "transfer-encoding"})


class WebhookSpec(BaseModel):
    """The configuration of one `webhook` effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="Identifies this destination; one delivery per mail per name.")
    url: str
    method: Literal["POST", "PUT"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    received_at_param: str | None = None
    body: Literal["raw_message"] = "raw_message"

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        if not WEBHOOK_NAME.match(value):
            raise ValueError("name must be lowercase letters, digits, '-' or '_' (max 63)")
        return value

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("url must be an absolute http or https URL")
        if parts.username or parts.password:
            raise ValueError("url must not carry credentials; use a header with a secret")
        return value

    @field_validator("received_at_param")
    @classmethod
    def _check_received_at_param(cls, value: str | None) -> str | None:
        if value is not None and not _QUERY_KEY.match(value):
            raise ValueError("received_at_param must be a plain query parameter name")
        return value

    @model_validator(mode="after")
    def _check_headers(self) -> WebhookSpec:
        for header, value in self.headers.items():
            if not _HEADER_NAME.match(header):
                raise ValueError(f"invalid header name {header!r}")
            if header.lower() in _RESERVED_HEADERS:
                raise ValueError(f"header {header!r} is set by the delivery itself")
            if "\r" in value or "\n" in value:
                raise ValueError(f"header {header!r} has a line break in its value")
            if _CREDENTIAL_HEADER.search(header) and not SECRET_REFERENCE.search(value):
                raise ValueError(
                    f"header {header!r} looks like a credential and must reference a "
                    "stored secret as {{secret:NAME}}"
                )
        return self


def referenced_secrets(headers: Mapping[str, Any]) -> list[str]:
    """Every secret name the header values reference, in order, once each."""
    seen: dict[str, None] = {}
    for value in headers.values():
        for name in SECRET_REFERENCE.findall(str(value)):
            seen.setdefault(name)
    return list(seen)


def render_headers(headers: Mapping[str, str], secrets: Mapping[str, str]) -> dict[str, str]:
    """Substitute every `{{secret:NAME}}` with its value from `secrets`.

    Raises:
        KeyError: a referenced name is absent from `secrets` (the key is
            the secret's name, never a value)
    """
    return {
        header: SECRET_REFERENCE.sub(lambda m: secrets[m.group(1)], value)
        for header, value in headers.items()
    }
