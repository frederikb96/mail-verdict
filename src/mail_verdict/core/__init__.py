"""MailVerdict core infrastructure."""

from mail_verdict.core.jsonb import parse_jsonb
from mail_verdict.core.logging import setup_logging

__all__ = [
    "parse_jsonb",
    "setup_logging",
]
