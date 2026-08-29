#!/usr/bin/env python3
"""Deliver the test email corpus into the development mail server.

The development stack runs a Dovecot container as a throwaway mail world. Any username
authenticates against one shared password and its mailbox is created on first login, so
there is no account provisioning step -- mail just needs to be delivered.

Delivery goes over LMTP, the same path a real mail transfer agent would use, so the
messages arrive as genuinely inbound mail rather than being injected into the database.

    python scripts/seed_dev.py
    python scripts/seed_dev.py --to bob@test.local --folder INBOX

Run it after the development stack is up. It is idempotent in the sense that running it
twice delivers the corpus twice -- useful for generating volume, occasionally surprising.
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "tests" / "fixtures" / "emails"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_LMTP_PORT = 24024
DEFAULT_RECIPIENT = "alice@test.local"


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


def deliver(message: bytes, host: str, port: int, sender: str, recipient: str) -> None:
    """Deliver one message over LMTP.

    The development mail server speaks implicit TLS on its LMTP port -- a plain socket
    simply hangs waiting for a greeting that never arrives in the clear. The certificate
    is self-signed, so verification is off; this only ever talks to a local container.
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
                ("LHLO seed.local", "250"),
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


def load_corpus() -> list[tuple[str, bytes]]:
    """Return every fixture email as (name, RFC822 bytes with CRLF line endings)."""
    if not CORPUS.is_dir():
        raise SystemExit(f"No corpus at {CORPUS}")
    messages: list[tuple[str, bytes]] = []
    for path in sorted(CORPUS.glob("*.eml")):
        raw = path.read_bytes()
        # Fixtures are stored with plain newlines; the wire needs CRLF.
        normalised = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        messages.append((path.name, normalised))
    return messages


def main() -> int:
    """Deliver the corpus and report what landed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST, help="mail server host")
    parser.add_argument(
        "--lmtp-port", type=int, default=DEFAULT_LMTP_PORT, help="LMTP port (implicit TLS)"
    )
    parser.add_argument("--to", default=DEFAULT_RECIPIENT, help="recipient address")
    parser.add_argument(
        "--from", dest="sender", default="sender@example.com", help="envelope sender"
    )
    args = parser.parse_args()

    corpus = load_corpus()
    if not corpus:
        raise SystemExit(f"No .eml files in {CORPUS}")

    delivered = 0
    for name, message in corpus:
        try:
            deliver(message, args.host, args.lmtp_port, args.sender, args.to)
        except (LmtpError, OSError, ssl.SSLError) as exc:
            print(f"  {name}: FAILED — {exc}", file=sys.stderr)
            continue
        print(f"  {name}")
        delivered += 1

    print(f"\nDelivered {delivered}/{len(corpus)} messages to {args.to}.")

    if delivered == 0:
        print(
            "\nNothing was delivered. Check the development stack is running and that "
            f"the LMTP port is reachable at {args.host}:{args.lmtp_port}.",
            file=sys.stderr,
        )
        return 1

    print(
        "\nAdd the account in the interface, or through the API, using this mailbox as "
        "both the IMAP user and the address. PostIMAP picks up a new account without a "
        "restart and the messages appear as it syncs."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
