"""
Utility for handling double-encoded JSONB values from PostIMAP.

PostIMAP stores certain JSONB columns as JSON strings inside JSONB --
effectively double-serialized. When SQLAlchemy reads them, Python gets
a ``str`` instead of a ``dict``/``list``. This module provides a helper
to transparently unwrap such values.
"""

from __future__ import annotations

import json
from typing import Any


def parse_jsonb(value: Any) -> Any:
    """
    Parse a JSONB value that might be double-encoded as a JSON string.

    PostIMAP sometimes stores ``'{"key":"val"}'`` (a JSON string) inside
    a JSONB column rather than ``{"key":"val"}`` (a JSON object). This
    function detects the string case and deserializes it.

    Args:
        value: Raw value from SQLAlchemy ORM (may be dict, list, str, or None)

    Returns:
        Parsed Python object (dict/list) if the input was a JSON string,
        otherwise the original value unchanged.
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value
