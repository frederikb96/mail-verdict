"""
Extraction in a child process. PDFium and Tesseract parse untrusted files and
a scan can take seconds of CPU; a thread would still hold the interpreter lock
against the event loop, and a crash would take the API down. One spawned child
does the work: a document that outruns its budget or kills the child costs the
child, never the API -- the pool is replaced and the document comes back as
method `failed`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import get_context

from mail_verdict.config.loader import ExtractionConfig
from mail_verdict.orders.extract.model import ExtractedDocument

logger = logging.getLogger(__name__)

Reader = Callable[[bytes, str, str, ExtractionConfig], ExtractedDocument]


def read_in_child(
    data: bytes, content_type: str, filename: str, limits: ExtractionConfig,
) -> ExtractedDocument:
    """The default reader: imports the heavy libraries in the child, not the API."""
    from mail_verdict.orders.extract.reader import extract_bytes

    return extract_bytes(data, content_type, filename, limits)


class ExtractionPool:
    """A one-child process pool that reads documents with a time budget."""

    def __init__(self, limits: ExtractionConfig, reader: Reader = read_in_child) -> None:
        self._limits = limits
        self._reader = reader
        self._executor: ProcessPoolExecutor | None = None
        self._lock = asyncio.Lock()

    def _ensure(self) -> ProcessPoolExecutor:
        if self._executor is None:
            self._executor = ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn"))
        return self._executor

    def _discard(self) -> None:
        """Kill the child (it may still be grinding on the document) and drop the pool."""
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.kill_workers()
            executor.shutdown(wait=False, cancel_futures=True)

    async def extract(self, data: bytes, content_type: str, filename: str) -> ExtractedDocument:
        """
        Read one document in the child.

        Documents are read one at a time; concurrent callers wait their turn.

        Args:
            data: The file's bytes
            content_type: Its declared MIME type
            filename: Its file name

        Returns:
            The document; method `failed` with `error` `timeout` or `crash`
            when the child outran `timeout_seconds` or died
        """
        async with self._lock:
            loop = asyncio.get_running_loop()
            try:
                future = loop.run_in_executor(
                    self._ensure(), self._reader, data, content_type, filename, self._limits,
                )
                return await asyncio.wait_for(future, timeout=self._limits.timeout_seconds)
            except TimeoutError:
                reason = "timeout"
            except BrokenProcessPool:
                reason = "crash"
            logger.warning("Extraction of %r failed: %s; replacing the child", filename, reason)
            self._discard()
            return ExtractedDocument(
                filename=filename, content_type=content_type, size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(), method="failed", error=reason,
            )

    def close(self) -> None:
        """Stop the child. Safe to call twice."""
        self._discard()
