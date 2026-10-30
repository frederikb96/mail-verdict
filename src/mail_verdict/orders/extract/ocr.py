"""
OCR through the Tesseract command line, for pages and images with no text
layer. Tesseract is a subprocess with its own time limit, one thread, and
reads the image from stdin; its TSV output carries per-word confidence and
position, which gives the text its lines and lets a barcode be placed.

Why Tesseract: on 9 real PDFs its word recall against their own text layers
was 0.97 at 2.5 to 5 CPU seconds and under 100 MB per page; the ONNX-based
alternatives need a wheel this Python version does not have and 600 MB.
"""

from __future__ import annotations

import io
import os
import subprocess
from dataclasses import dataclass, replace

from PIL import Image

# Below this mean word confidence the page is tried again turned 180 degrees.
# Over 14 real pages, upright ones scored 86 to 95 and the same pages turned
# upside down 38 to 52; a rotated label read 41.7 upright-wrong and 92.8 turned
# back, so a threshold of 40 would have kept the garbage.
RETRY_ROTATED_BELOW = 75.0
# Pages and photos are scaled down to this long side before OCR.
MAX_SIDE = 4_000

_COLUMNS = 12


class OcrError(RuntimeError):
    """Tesseract is missing, a language is not installed, or a call failed."""


@dataclass(frozen=True)
class OcrLine:
    """One recognised line: its text and top-left corner in the image."""

    text: str
    top: float
    left: float
    paragraph_start: bool


@dataclass(frozen=True)
class OcrResult:
    """`confidence` is the mean word confidence (0-100), 0 when no word was read.
    `scale` is the size of the OCR'd image over the given one (line positions
    are in the OCR'd image); `rotated` is set when the 180-degree retry won,
    so positions do not refer to the image as given at all."""

    lines: tuple[OcrLine, ...]
    confidence: float
    scale: float = 1.0
    rotated: bool = False

    def _rows(self) -> list[tuple[str, OcrLine]]:
        rows: list[tuple[str, OcrLine]] = []
        for line in self.lines:
            if line.paragraph_start and rows:
                rows.append(("", line))
            rows.append((line.text, line))
        return rows

    @property
    def text(self) -> str:
        """The lines joined, a blank line between paragraphs."""
        return "\n".join(text for text, _ in self._rows())

    def anchors(self, pixels_per_unit: float = 1.0) -> list[tuple[int, float, float]]:
        """(index in `text`, top, left) of each line, in units of the image as
        given (`pixels_per_unit` is that image's pixels per unit)."""
        out = []
        offset = 0
        divisor = self.scale * pixels_per_unit
        for text, line in self._rows():
            if text:
                out.append((offset, line.top / divisor, line.left / divisor))
            offset += len(text) + 1
        return out


def _run(image: Image.Image, languages: str, timeout: float) -> OcrResult:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
    try:
        done = subprocess.run(
            [
                "tesseract", "stdin", "stdout", "-l", languages, "--psm", "3",
                "-c", "tessedit_create_tsv=1",
            ],
            input=buffer.getvalue(), capture_output=True, timeout=timeout, env=env, check=False,
        )
    except FileNotFoundError as exc:
        raise OcrError("tesseract is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise OcrError(f"tesseract timed out after {timeout:g}s") from exc
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()[:300]
        raise OcrError(f"tesseract failed ({languages}): {detail}")
    output = done.stdout.decode("utf-8", "replace")
    if not output.strip():
        raise OcrError(f"tesseract produced no output ({languages})")
    return _parse_tsv(output)


def _parse_tsv(tsv: str) -> OcrResult:
    words: dict[tuple[str, str, str], list[tuple[int, int, str]]] = {}
    confidences: list[float] = []
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < _COLUMNS or cols[0] != "5" or not cols[11].strip():
            continue
        confidence = float(cols[10])
        if confidence < 0:
            continue
        confidences.append(confidence)
        words.setdefault((cols[2], cols[3], cols[4]), []).append(
            (int(cols[6]), int(cols[7]), cols[11].strip()),
        )
    lines: list[OcrLine] = []
    last_paragraph: tuple[str, str] | None = None
    for (block, paragraph, _line), parts in words.items():
        lines.append(OcrLine(
            text=" ".join(p[2] for p in parts),
            top=float(min(p[1] for p in parts)), left=float(min(p[0] for p in parts)),
            paragraph_start=(block, paragraph) != last_paragraph,
        ))
        last_paragraph = (block, paragraph)
    mean = sum(confidences) / len(confidences) if confidences else 0.0
    return OcrResult(lines=tuple(lines), confidence=mean)


def ocr_image(image: Image.Image, *, languages: str, timeout: float) -> OcrResult:
    """
    Read the text of an image.

    Args:
        image: A page render, scan or photo
        languages: Tesseract languages joined with `+`; all must be installed
        timeout: Seconds one Tesseract call may take

    Returns:
        The recognised lines; retried upside down when the first read is poor

    Raises:
        OcrError: Tesseract is absent, a language is missing, or it timed out
    """
    work = image.convert("L")
    if max(work.size) > MAX_SIDE:
        work.thumbnail((MAX_SIDE, MAX_SIDE))
    scale = work.width / image.width
    first = replace(_run(work, languages, timeout), scale=scale)
    if first.confidence >= RETRY_ROTATED_BELOW:
        return first
    second = _run(work.rotate(180), languages, timeout)
    if second.confidence > first.confidence:
        return replace(second, scale=scale, rotated=True)
    return first
