"""
orders/extract/pool.py: documents are read in a child process that may time out
or die without touching the API, and the pool replaces it afterwards.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import pytest

from mail_verdict.config.loader import ExtractionConfig
from mail_verdict.orders.extract.model import ExtractedDocument
from mail_verdict.orders.extract.pool import ExtractionPool
from tests.helpers.documents import limits, make_pdf


def _doc(filename: str, text: str = "", method: str = "text_layer") -> ExtractedDocument:
    return ExtractedDocument(
        filename=filename, content_type="", size_bytes=0, sha256="", method=method,  # type: ignore[arg-type]
        text=text,
    )


def read_pid(
    data: bytes, content_type: str, filename: str, cfg: ExtractionConfig,
) -> ExtractedDocument:
    """Reports which process read it."""
    return _doc(filename, text=str(os.getpid()))


def read_or_misbehave(
    data: bytes, content_type: str, filename: str, cfg: ExtractionConfig,
) -> ExtractedDocument:
    """`hang` never returns, `die` kills the process, anything else answers."""
    if data == b"hang":
        time.sleep(600)
    if data == b"die":
        os._exit(1)
    return _doc(filename, text=str(os.getpid()))


@pytest.fixture()
def pool():
    made: list[ExtractionPool] = []

    def make(reader, **overrides) -> ExtractionPool:  # type: ignore[no-untyped-def]
        created = ExtractionPool(limits(**overrides), reader)
        made.append(created)
        return created

    yield make
    for created in made:
        created.close()


@pytest.mark.asyncio
async def test_a_document_is_read_in_another_process(pool) -> None:  # type: ignore[no-untyped-def]
    doc = await pool(read_pid).extract(b"x", "", "a.bin")
    assert doc.method == "text_layer"
    assert int(doc.text) != os.getpid()


@pytest.mark.asyncio
async def test_the_same_child_serves_successive_documents(pool) -> None:  # type: ignore[no-untyped-def]
    extraction = pool(read_pid)
    first = await extraction.extract(b"x", "", "a.bin")
    second = await extraction.extract(b"x", "", "b.bin")
    assert first.text == second.text


@pytest.mark.asyncio
async def test_a_timeout_is_a_failed_document_and_the_pool_is_replaced(pool) -> None:  # type: ignore[no-untyped-def]
    extraction = pool(read_or_misbehave, timeout_seconds=1.5)
    warm = await extraction.extract(b"fine", "", "warm.bin")
    started = time.monotonic()
    hung = await extraction.extract(b"hang", "text/plain", "slow.bin")
    assert time.monotonic() - started < 10
    assert (hung.method, hung.error) == ("failed", "timeout")
    assert hung.filename == "slow.bin"
    assert hung.size_bytes == 4
    assert len(hung.sha256) == 64

    after = await extraction.extract(b"fine", "", "next.bin")
    assert after.method == "text_layer"
    assert after.text != warm.text, "a new child must have replaced the hung one"


@pytest.mark.asyncio
async def test_a_dying_child_is_a_failed_document_and_the_pool_is_replaced(pool) -> None:  # type: ignore[no-untyped-def]
    extraction = pool(read_or_misbehave)
    died = await extraction.extract(b"die", "", "bomb.bin")
    assert (died.method, died.error) == ("failed", "crash")
    after = await extraction.extract(b"fine", "", "next.bin")
    assert after.method == "text_layer"


@pytest.mark.asyncio
async def test_the_hung_child_is_killed_not_left_running(pool) -> None:  # type: ignore[no-untyped-def]
    extraction = pool(read_or_misbehave, timeout_seconds=1.5)
    await extraction.extract(b"fine", "", "warm.bin")
    pids = set(extraction._executor._processes)  # type: ignore[union-attr]
    await extraction.extract(b"hang", "", "slow.bin")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(_alive(p) for p in pids):
        await asyncio.sleep(0.1)
    assert not any(_alive(p) for p in pids)


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().split(") ")[-1].split()[0] != "Z"
    except FileNotFoundError:
        return False


@pytest.mark.asyncio
async def test_the_event_loop_keeps_running_while_the_child_works(pool) -> None:  # type: ignore[no-untyped-def]
    extraction = pool(read_or_misbehave, timeout_seconds=1.5)
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    ticker = asyncio.create_task(tick())
    await extraction.extract(b"hang", "", "slow.bin")
    ticker.cancel()
    assert ticks >= 15


@pytest.mark.asyncio
async def test_the_default_reader_extracts_a_real_document(pool) -> None:  # type: ignore[no-untyped-def]
    from mail_verdict.orders.extract.pool import read_in_child

    data = make_pdf([
        (72, 760, "Order number: 258285260829"), (72, 740, "Thank you for your purchase, see you"),
    ])
    doc = await pool(read_in_child).extract(data, "application/pdf", "t.pdf")
    assert doc.method == "text_layer"
    assert "258285260829" in doc.text


def test_importing_the_package_does_not_load_the_heavy_libraries() -> None:
    code = (
        "import sys, mail_verdict.orders.content, mail_verdict.orders.extract.pool;"
        "heavy = {'pypdfium2', 'zxingcpp', 'PIL'} & set(sys.modules);"
        "print(sorted(heavy))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == "[]"
