"""Paragraphs in, chunks out — the half of chunking that no format owns.

This was `parse.py`'s packing loop until a second source arrived. Everything
here operates on `(start, end, text)` triples and two constants; nothing in it
knows what a page is, what a heading is, or which file the offsets came from.
The format-specific halves stay with their formats: `parse.py` keeps PDF page
extraction, page-offset lookup and the GDPR-typography paragraph regex;
`chunk_docx.py` keeps the heading walk. The only thing they share is this.

An earlier draft of the plan proposed duplicating this instead, arguing that a
shared packer would have to carry both a page map and a heading map. It would
carry neither — the maps live in the callers, in the same position `_page_at`
already occupies — and duplicating it would have left two copies of
`TARGET_CHARS` that no test compares, so a later tuning of one would silently
ship two corpora with different granularity into one ranking index.

**`TARGET_CHARS` and `OVERLAP_CHARS` are defined here and nowhere else.**
`parse.py` re-exports them because `tests/test_ingest_parse.py` imports them
from there and that test pins the GDPR chunk boundaries the gold set is built
on — the fewer things that move underneath it, the clearer its signal.
"""

from __future__ import annotations

# ~500 characters, ~50 characters of overlap between consecutive chunks, cut at
# a paragraph boundary — the contract the citation panel already assumes
# (page-anchored excerpts of a few sentences each, the same shape as
# `seed/gdpr_excerpts.json`).
TARGET_CHARS = 500
OVERLAP_CHARS = 50


def split_long(start: int, text: str) -> list[tuple[int, int, str]]:
    """Hard-split one paragraph that alone exceeds `TARGET_CHARS`.

    Cuts at the nearest sentence end ("`. `") at or before the target, falling
    back to the nearest space so a boundary is never mid-word. Adds no overlap
    of its own: `pack_chunks` is the single place that adds the 50-character
    overlap, so a paragraph this function cuts in two gets exactly the overlap
    two independent paragraphs would — not twice that, which is what happens if
    both layers add one.
    """
    pieces: list[tuple[int, int, str]] = []
    cursor = start
    remaining = text
    while len(remaining) > TARGET_CHARS:
        window = remaining[:TARGET_CHARS]
        cut = window.rfind(". ")
        if cut < TARGET_CHARS // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = TARGET_CHARS
        else:
            cut += 1  # keep the period (or the word before the space) with the head
        head = remaining[:cut].strip()
        pieces.append((cursor, cursor + len(head), head))
        cursor += cut
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append((cursor, cursor + len(remaining), remaining))
    return pieces


def overlap_tail(text: str) -> str:
    """The last `OVERLAP_CHARS` of `text`, trimmed back to the first whole word
    so the overlap prefixed onto the next chunk never starts mid-word."""
    tail = text[-OVERLAP_CHARS:]
    space = tail.find(" ")
    return tail[space + 1 :] if space != -1 else tail


def pack_chunks(
    paragraphs: list[tuple[int, int, str]],
    *,
    flush_at: frozenset[int] = frozenset(),
) -> list[tuple[str, int, int]]:
    """Pack paragraphs into `(content, start_offset, end_offset)` chunks.

    `flush_at` is a set of start offsets at which the buffer must be closed
    before the paragraph is added, **and the usual overlap is not carried
    across**. It exists for `chunk_docx.py`, which labels every chunk with the
    heading in effect at its start: without it a chunk can straddle a heading,
    and worse, `overlap_tail` prefixes up to 50 characters of the *previous*
    section onto the next chunk, so a chunk whose start offset lands inside that
    tail would be labelled with the section it holds 50 characters of rather
    than the one it holds 450 of. The PDF path passes no `flush_at` and is
    unaffected — it carries a page *range* and does not have this problem.

    Deterministic: no randomness, no set iteration order dependence (`flush_at`
    is only ever membership-tested), nothing but the arguments.
    """
    prepared: list[tuple[int, int, str]] = []
    for start, end, text in paragraphs:
        if len(text) > TARGET_CHARS:
            prepared.extend(split_long(start, text))
        else:
            prepared.append((start, end, text))

    chunks: list[tuple[str, int, int]] = []
    buffer = ""
    buffer_start: int | None = None
    buffer_end: int | None = None

    def flush() -> None:
        nonlocal buffer, buffer_start, buffer_end
        if buffer:
            assert buffer_start is not None and buffer_end is not None
            chunks.append((buffer, buffer_start, buffer_end))
        buffer, buffer_start, buffer_end = "", None, None

    for start, end, text in prepared:
        if start in flush_at and buffer:
            flush()
            buffer, buffer_start, buffer_end = text, start, end
            continue

        candidate = f"{buffer} {text}".strip() if buffer else text
        if buffer and len(candidate) > TARGET_CHARS:
            prior_end = buffer_end
            flush()
            overlap = overlap_tail(chunks[-1][0])
            buffer = f"{overlap} {text}".strip() if overlap else text
            # The overlap is a copy of the tail just flushed, so the new chunk's
            # first character is, physically, still where the flushed chunk
            # ended — which is what keeps the caller's offset lookup honest.
            buffer_start = prior_end
            buffer_end = end
        else:
            if buffer_start is None:
                buffer_start = start
            buffer = candidate
            buffer_end = end
    flush()

    return chunks
