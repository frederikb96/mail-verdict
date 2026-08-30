"""
Raw LMTP delivery into the test Dovecot -- simulates mail arriving from an
external sender, the same path scripts/seed_dev.py uses against the
development stack.
"""

from __future__ import annotations

import socket
import ssl


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
) -> bytes:
    """Build a minimal RFC822 message with CRLF line endings for LMTP delivery."""
    headers = [
        f"From: {sender}",
        f"To: {recipient}",
        f"Subject: {subject}",
    ]
    if message_id is not None:
        headers.append(f"Message-ID: {message_id}")
    headers += ["MIME-Version: 1.0", "Content-Type: text/plain; charset=utf-8"]
    lines = [*headers, "", body, ""]
    return "\r\n".join(lines).encode("utf-8")
