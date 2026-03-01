"""
Extension layer on top of aioimaplib.

Adds CONDSTORE/QRESYNC SELECT parameters, SPECIAL-USE LIST parsing,
and raw command execution that aioimaplib doesn't natively support.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from aioimaplib import IMAP4_SSL, Command, Response

logger = logging.getLogger(__name__)

# RFC 6154 special-use flag to SpecialUse enum value mapping
SPECIAL_USE_FLAGS: dict[str, str] = {
    "\\All": "all",
    "\\Archive": "archive",
    "\\Drafts": "drafts",
    "\\Flagged": "flagged",
    "\\Junk": "junk",
    "\\Sent": "sent",
    "\\Trash": "trash",
}

# Pattern: * LIST (\flags) "separator" "name"
_LIST_RESPONSE_RE = re.compile(
    rb'\* LIST \(([^)]*)\) "([^"]*)" (.+)',
)


@dataclass
class SelectResult:
    """Parsed SELECT response with CONDSTORE/QRESYNC extensions."""

    ok: bool
    exists: int = 0
    recent: int = 0
    uidvalidity: int = 0
    uidnext: int = 0
    highestmodseq: int | None = None
    flags: list[str] = field(default_factory=list)
    permanent_flags: list[str] = field(default_factory=list)
    raw_lines: list[bytes] = field(default_factory=list)


@dataclass
class FolderInfo:
    """Parsed LIST response for a single folder."""

    name: str
    separator: str
    flags: list[str] = field(default_factory=list)
    special_use: str | None = None


def _parse_select_response(response: Response) -> SelectResult:
    """
    Parse a SELECT response including CONDSTORE/QRESYNC extensions.

    Args:
        response: Raw IMAP response from SELECT
    """
    result = SelectResult(
        ok=response.result == "OK",
        raw_lines=list(response.lines),
    )

    for line in response.lines:
        text = line.decode(errors="replace") if isinstance(line, bytes) else str(line)
        upper = text.upper()

        if "EXISTS" in upper:
            match = re.search(r"(\d+)\s+EXISTS", upper)
            if match:
                result.exists = int(match.group(1))

        elif "RECENT" in upper:
            match = re.search(r"(\d+)\s+RECENT", upper)
            if match:
                result.recent = int(match.group(1))

        elif "UIDVALIDITY" in upper:
            match = re.search(r"UIDVALIDITY\s+(\d+)", upper)
            if match:
                result.uidvalidity = int(match.group(1))

        elif "UIDNEXT" in upper:
            match = re.search(r"UIDNEXT\s+(\d+)", upper)
            if match:
                result.uidnext = int(match.group(1))

        elif "HIGHESTMODSEQ" in upper:
            match = re.search(r"HIGHESTMODSEQ\s+(\d+)", upper)
            if match:
                result.highestmodseq = int(match.group(1))

        elif "FLAGS" in upper and "PERMANENTFLAGS" not in upper:
            match = re.search(r"FLAGS\s*\(([^)]*)\)", text)
            if match:
                result.flags = match.group(1).split()

        elif "PERMANENTFLAGS" in upper:
            match = re.search(r"PERMANENTFLAGS\s*\(([^)]*)\)", text)
            if match:
                result.permanent_flags = match.group(1).split()

    return result


def _parse_list_response(response: Response) -> list[FolderInfo]:
    """
    Parse LIST response lines into FolderInfo objects.

    Handles both standard and SPECIAL-USE flags.

    Args:
        response: Raw IMAP response from LIST
    """
    folders: list[FolderInfo] = []

    for line in response.lines:
        if not isinstance(line, bytes):
            continue

        match = _LIST_RESPONSE_RE.match(line)
        if not match:
            continue

        flags_raw = match.group(1).decode(errors="replace")
        separator = match.group(2).decode(errors="replace")
        name_raw = match.group(3).decode(errors="replace").strip().strip('"')

        flags = [f.strip() for f in flags_raw.split() if f.strip()]

        special_use: str | None = None
        for flag in flags:
            if flag in SPECIAL_USE_FLAGS:
                special_use = SPECIAL_USE_FLAGS[flag]
                break

        folders.append(
            FolderInfo(
                name=name_raw,
                separator=separator,
                flags=flags,
                special_use=special_use,
            )
        )

    return folders


class AsyncIMAPExtended:
    """
    Wraps aioimaplib IMAP4_SSL with extended protocol support.

    Provides CONDSTORE SELECT, QRESYNC SELECT, SPECIAL-USE LIST,
    and raw command execution.
    """

    def __init__(self, client: IMAP4_SSL) -> None:
        """
        Initialize with an existing aioimaplib client.

        Args:
            client: Connected and authenticated IMAP4_SSL instance
        """
        self._client = client

    @property
    def client(self) -> IMAP4_SSL:
        """Access the underlying aioimaplib client."""
        return self._client

    @property
    def capabilities(self) -> set[str]:
        """Get server capabilities."""
        return self._client.protocol.capabilities  # type: ignore[no-any-return]

    def has_capability(self, cap: str) -> bool:
        """
        Check if server advertises a capability.

        Args:
            cap: Capability name (e.g., 'CONDSTORE', 'QRESYNC')
        """
        return cap in self.capabilities

    async def select_condstore(self, mailbox: str = "INBOX") -> SelectResult:
        """
        SELECT with CONDSTORE extension (RFC 7162).

        Asks the server to include HIGHESTMODSEQ in the response.

        Args:
            mailbox: Mailbox to select
        """
        response = await self._raw_command("SELECT", mailbox, "(CONDSTORE)")
        return _parse_select_response(response)

    async def select_qresync(
        self,
        mailbox: str,
        uidvalidity: int,
        highestmodseq: int,
        *,
        known_uids: str | None = None,
    ) -> SelectResult:
        """
        SELECT with QRESYNC extension (RFC 7162).

        Allows the server to send VANISHED and flag changes since last sync.

        Args:
            mailbox: Mailbox to select
            uidvalidity: Last known UIDVALIDITY
            highestmodseq: Last known HIGHESTMODSEQ
            known_uids: Optional UID set of known messages
        """
        qresync_parts = [str(uidvalidity), str(highestmodseq)]
        if known_uids:
            qresync_parts.append(known_uids)
        qresync_param = f"(QRESYNC ({' '.join(qresync_parts)}))"

        response = await self._raw_command("SELECT", mailbox, qresync_param)
        return _parse_select_response(response)

    async def select_plain(self, mailbox: str = "INBOX") -> SelectResult:
        """
        Standard SELECT without extensions.

        Args:
            mailbox: Mailbox to select
        """
        response = await self._client.select(mailbox)
        return _parse_select_response(response)

    async def list_folders(
        self,
        reference: str = "",
        pattern: str = "*",
    ) -> list[FolderInfo]:
        """
        LIST mailboxes.

        Args:
            reference: Reference name (usually empty)
            pattern: Mailbox pattern (default "*" for all)
        """
        response = await self._client.list(reference, pattern)
        if response.result != "OK":
            logger.warning(
                "LIST command failed",
                extra={"result": response.result},
            )
            return []
        return _parse_list_response(response)

    async def list_special_use(
        self,
        reference: str = "",
        pattern: str = "*",
    ) -> list[FolderInfo]:
        """
        LIST with SPECIAL-USE RETURN option (RFC 6154).

        Falls back to regular LIST if SPECIAL-USE not supported.

        Args:
            reference: Reference name
            pattern: Mailbox pattern
        """
        if self.has_capability("SPECIAL-USE"):
            response = await self._raw_command(
                "LIST", reference or '""', pattern, "RETURN (SPECIAL-USE)"
            )
            if response.result == "OK":
                return _parse_list_response(response)
            logger.warning("SPECIAL-USE LIST failed, falling back to regular LIST")

        return await self.list_folders(reference, pattern)

    async def enable_qresync(self) -> bool:
        """
        ENABLE QRESYNC (requires ENABLE + QRESYNC capabilities).

        Returns:
            True if QRESYNC was enabled successfully
        """
        if not self.has_capability("QRESYNC"):
            return False
        if not self.has_capability("ENABLE"):
            return False

        response = await self._client.enable("QRESYNC")
        ok: bool = response.result == "OK"
        if ok:
            logger.info("QRESYNC enabled")
        else:
            logger.warning("Failed to enable QRESYNC")
        return ok

    async def _raw_command(self, name: str, *args: str) -> Response:
        """
        Execute a raw IMAP command via the protocol layer.

        This bypasses aioimaplib's higher-level wrappers to allow
        custom parameters (CONDSTORE, QRESYNC, RETURN options).

        Args:
            name: IMAP command name
            *args: Command arguments
        """
        cmd = Command(
            name,
            self._client.protocol.new_tag(),
            *args,
            loop=self._client.protocol.loop,
        )
        response = await self._client.protocol.execute(cmd)
        return response
