"""
The envelope sender: the address a message was actually sent from at the
SMTP level (MAIL FROM), as opposed to the From header written into the
message itself. The receiving server records it as the Return-Path header.

From is free text the sender chooses; the envelope sender is where bounces
go and what SPF checks, so the two disagreeing is worth showing -- though a
newsletter or mailing service legitimately uses its own bounce address.
"""

from __future__ import annotations

from email.utils import parseaddr
from typing import Any

from sqlalchemy.orm import with_expression
from sqlalchemy.orm.interfaces import LoaderOption

# PostIMAP stores raw_headers keyed by lower-case header name.
RETURN_PATH_HEADER = "return-path"


def envelope_sender(return_path: str | None) -> str | None:
    """The bare address out of a Return-Path header value -- None when the
    header is missing or carries the null sender `<>` (a bounce)."""
    if not return_path:
        return None
    return parseaddr(return_path)[1] or None


def return_path_option(model: Any) -> LoaderOption:
    """Loader option filling `model.return_path` from raw_headers in SQL, so
    a query that defers raw_headers still gets this one header."""
    return with_expression(model.return_path, model.raw_headers[RETURN_PATH_HEADER].astext)
