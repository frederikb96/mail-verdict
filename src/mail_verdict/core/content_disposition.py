"""
Content-Disposition filename encoding.

An HTTP header value has to be Latin-1: a filename carrying a character
outside that range (CJK, Cyrillic, most emoji, and plenty of ordinary
attachment names from a non-Latin-1 sender) makes the plain
`filename="..."` form raise when the response is written, turning a
download into a 500 rather than a wrong-but-working name.
"""

from __future__ import annotations

from urllib.parse import quote


def content_disposition(filename: str, *, disposition: str = "attachment") -> str:
    """
    Build a Content-Disposition header value safe for any filename.

    Sends both forms, per RFC 6266: an ASCII fallback a client without
    RFC 5987 support falls back to, and filename*=UTF-8''... (percent-
    encoded) that every current browser prefers over it.

    Args:
        filename: The attachment's name, any Unicode
        disposition: "attachment" or "inline"

    Returns:
        A header value safe to pass to an ASGI response
    """
    filename = "".join(c if c.isprintable() else "_" for c in filename)
    ascii_fallback = filename.encode("ascii", "replace").decode("ascii").replace("?", "_")
    encoded = quote(filename, safe="")
    return f'{disposition}; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'
