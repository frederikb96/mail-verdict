"""
Raw LMTP delivery into the test Dovecot -- simulates mail arriving from an
external sender, the same path scripts/seed_dev.py uses against the
development stack.
"""

from __future__ import annotations

import email.utils
import socket
import ssl
from email.parser import BytesParser
from pathlib import Path

CORPUS_DIR = Path(__file__).parent.parent / "fixtures" / "emails"


class LmtpError(RuntimeError):
    """The mail server rejected something during delivery."""


def _read_reply(stream: object) -> str:
    """Read one LMTP reply, following continuation lines."""
    lines: list[str] = []
    while True:
        raw = stream.readline()  # type: ignore[attr-defined]
        if not raw:
            raise LmtpError("connection closed mid-reply")
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        lines.append(line)
        # A continuation line has a dash in the fourth column; the final one has a space.
        if len(line) < 4 or line[3] != "-":
            return "\n".join(lines)


def deliver_message(
    message: bytes, host: str, port: int, *, sender: str, recipient: str,
) -> None:
    """Deliver one RFC822 message over LMTP.

    The test mail server speaks implicit TLS on its LMTP port -- a plain
    socket simply hangs waiting for a greeting that never arrives in the
    clear. The certificate is self-signed, so verification is off; this
    only ever talks to a throwaway test container.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    with socket.create_connection((host, port), timeout=20) as raw_sock:
        with context.wrap_socket(raw_sock) as sock:
            stream = sock.makefile("rwb")

            greeting = _read_reply(stream)
            if not greeting.startswith("220"):
                raise LmtpError(f"unexpected greeting: {greeting}")

            for command, expected in (
                ("LHLO test.local", "250"),
                (f"MAIL FROM:<{sender}>", "250"),
                (f"RCPT TO:<{recipient}>", "250"),
                ("DATA", "354"),
            ):
                stream.write(f"{command}\r\n".encode())
                stream.flush()
                reply = _read_reply(stream)
                if not reply.startswith(expected):
                    raise LmtpError(f"{command!r} -> {reply}")

            # Dot-stuffing: a line that is a single dot would otherwise end the message.
            body = message.replace(b"\r\n.\r\n", b"\r\n..\r\n")
            stream.write(body)
            if not body.endswith(b"\r\n"):
                stream.write(b"\r\n")
            stream.write(b".\r\n")
            stream.flush()
            reply = _read_reply(stream)
            if not reply.startswith("250"):
                raise LmtpError(f"message rejected: {reply}")

            stream.write(b"QUIT\r\n")
            stream.flush()


def build_eml(
    *,
    sender: str,
    recipient: str,
    subject: str,
    body: str = "Test message body.",
    message_id: str | None = None,
    content_type: str = "text/plain; charset=utf-8",
) -> bytes:
    """Build a minimal RFC822 message with CRLF line endings for LMTP delivery."""
    headers = [
        f"From: {sender}",
        f"To: {recipient}",
        f"Subject: {subject}",
    ]
    if message_id is not None:
        headers.append(f"Message-ID: {message_id}")
    headers += ["MIME-Version: 1.0", f"Content-Type: {content_type}"]
    lines = [*headers, "", body, ""]
    return "\r\n".join(lines).encode("utf-8")


def freshen(raw: bytes) -> bytes:
    """Restamp a fixture as mail arriving now: current Date, unique Message-ID.

    Parsed and reserialised rather than pattern-matched, so a folded header or a
    multipart body is rewritten the way the fixture actually structures it. Only the
    two headers are touched; every other byte survives the round trip.
    """
    message = BytesParser().parsebytes(raw)
    del message["Date"]
    message["Date"] = email.utils.formatdate(localtime=True)
    del message["Message-ID"]
    message["Message-ID"] = email.utils.make_msgid(domain="test.local")
    return message.as_bytes()


def load_corpus(*, keep_dates: bool = False) -> list[tuple[str, bytes]]:
    """Return every fixture email as (name, RFC822 bytes with CRLF line endings).

    Fixtures are stamped with the current date and a fresh Message-ID by default,
    which is what makes the pipeline treat them as mail that just landed and
    classify it; a repeated Message-ID resolves to an already-verdicted message and
    is silently never classified again. `keep_dates` delivers them byte-for-byte.
    """
    if not CORPUS_DIR.is_dir():
        raise SystemExit(f"No corpus at {CORPUS_DIR}")
    messages: list[tuple[str, bytes]] = []
    for path in sorted(CORPUS_DIR.glob("*.eml")):
        raw = path.read_bytes()
        if not keep_dates:
            raw = freshen(raw)
        # Fixtures are stored with plain newlines; the wire needs CRLF.
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        messages.append((path.name, normalised))
    return messages
