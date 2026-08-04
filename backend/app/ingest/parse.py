"""A deterministic PDF parser and chunker.

The parser exists to give every chunk of every catalogue source a stable
identity — `(source_key, chunk_ordinal)` — that survives a re-parse in a
different process, on a different machine, on a different day. That is a
stronger requirement than "the same input gives the same output": a pure
function already has that property whether it is correct or not (see
`test_the_chunking_is_the_one_the_gold_set_was_built_against` in
`tests/test_ingest_parse.py`). What can actually change the output from one
run to the next is the chunk size, the overlap, the paragraph rule, or
pypdf's own extraction across a version bump — which is exactly why
`pypdf` is pinned exactly (`pyproject.toml`) and why the chunk boundaries
are pinned as data in a committed fixture, not re-derived by a test that
would pass for a broken chunker just as readily as a working one.

Citations already carry `page_from`/`page_to` and the panel already shows
them (`frontend/components/sources.tsx`); this parser keeps that contract by
tracking, for every chunk, which page of the source PDF its first and last
character came from.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass

import pypdf

from app.ingest.catalogue import Source

# ~500 characters, ~50 characters of overlap between consecutive chunks, cut
# at a paragraph boundary — the contract the existing citation panel already
# assumes (page-anchored excerpts of a few sentences each, the same shape as
# `seed/gdpr_excerpts.json`).
TARGET_CHARS = 500
OVERLAP_CHARS = 50

_WHITESPACE = re.compile(r"\s+")

# What counts as "a paragraph boundary" in this text. pypdf's extraction of
# this particular PDF carries no blank line between paragraphs — the
# typesetting is fully justified running text with no paragraph-level markup
# surviving extraction — so a boundary is defined the way the document itself
# marks one: the start of a numbered recital ("(30) "), an article header
# ("Article 4 "), a numbered paragraph within an article ("1. "), or a
# lettered sub-point ("(a) "). Deliberately excludes footnote markers like
# "( 1 )": pypdf renders the superscript reference with a line break either
# side of the digit, which normalises to a space inside the parentheses —
# "( 1 )", not "(1)" — so the pattern below (no internal space) never matches
# one.
_PARAGRAPH_BOUNDARY = re.compile(
    r"\(\d{1,3}\)\s"  # recitals: "(30) "
    r"|Article\s\d+\s"  # article headers: "Article 4 "
    r"|\b\d{1,2}\.\s"  # numbered paragraphs: "1. "
    r"|\([a-z]\)\s"  # lettered sub-points: "(a) "
)


@dataclass(frozen=True)
class Chunk:
    chunk_ordinal: int
    content: str
    page_from: int
    page_to: int


def _extract_pages(source: Source) -> list[str]:
    """One normalised string per page: internal whitespace collapsed to single
    spaces, so page text can be concatenated and searched without a stray
    newline splitting a word pypdf itself kept on one line."""
    reader = pypdf.PdfReader(str(source.path))
    return [_WHITESPACE.sub(" ", (page.extract_text() or "")).strip() for page in reader.pages]


def _concatenate(pages: list[str]) -> tuple[str, list[int]]:
    """Join every page into one string with a single joining space, and
    return the offset each page starts at in that string — the lookup table
    `_page_at` walks to turn a character offset back into a page number."""
    parts: list[str] = []
    page_starts: list[int] = []
    offset = 0
    for text in pages:
        page_starts.append(offset)
        parts.append(text)
        offset += len(text) + 1
    return " ".join(parts), page_starts


def _page_at(offset: int, page_starts: list[int]) -> int:
    """1-indexed page number containing `offset`, matching the OJ page
    numbers `seed/gdpr_excerpts.json` already uses for `page_from`/`page_to`."""
    return max(bisect_right(page_starts, offset) - 1, 0) + 1


def _paragraphs(full_text: str) -> list[tuple[int, int, str]]:
    """Split the whole document into (start_offset, end_offset, text)
    paragraphs at every `_PARAGRAPH_BOUNDARY` match, always starting the
    first paragraph at offset 0 even though nothing marks its start."""
    starts = sorted({0} | {match.start() for match in _PARAGRAPH_BOUNDARY.finditer(full_text)})
    paragraphs = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(full_text)
        text = full_text[start:end].strip()
        if text:
            paragraphs.append((start, end, text))
    return paragraphs


def _split_long(start: int, text: str) -> list[tuple[int, int, str]]:
    """Hard-split one paragraph that alone exceeds `TARGET_CHARS`.

    Cuts at the nearest sentence end ("`. `") at or before the target,
    falling back to the nearest space so a boundary is never mid-word. Adds
    no overlap of its own: the packing loop in `parse_source` is the single
    place that adds the 50-character overlap, so a paragraph this function
    cuts in two gets exactly the overlap two independent paragraphs would —
    not twice that, which is what happens if both layers add one.
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


def _overlap_tail(text: str) -> str:
    """The last `OVERLAP_CHARS` of `text`, trimmed back to the first whole
    word so the overlap prefixed onto the next chunk never starts mid-word."""
    tail = text[-OVERLAP_CHARS:]
    space = tail.find(" ")
    return tail[space + 1 :] if space != -1 else tail


def parse_source(source: Source) -> list[Chunk]:
    """Parse `source.path` into chunks with page-anchored, ordinal identity.

    Deterministic by construction: no randomness, no set iteration, no
    wall-clock or filesystem-order dependency — the only inputs are the PDF
    bytes and the constants above. `chunk_ordinal` is the chunk's index in
    the returned list, in document reading order, which is what
    `test_the_chunking_is_the_one_the_gold_set_was_built_against` pins
    against a committed fixture.
    """
    pages = _extract_pages(source)
    full_text, page_starts = _concatenate(pages)

    paragraphs: list[tuple[int, int, str]] = []
    for start, _end, text in _paragraphs(full_text):
        if len(text) > TARGET_CHARS:
            paragraphs.extend(_split_long(start, text))
        else:
            paragraphs.append((start, _end, text))

    chunks: list[tuple[str, int, int]] = []  # (content, start_offset, end_offset)
    buffer = ""
    buffer_start: int | None = None
    buffer_end: int | None = None

    def flush() -> None:
        nonlocal buffer, buffer_start, buffer_end
        if buffer:
            assert buffer_start is not None and buffer_end is not None
            chunks.append((buffer, buffer_start, buffer_end))
        buffer, buffer_start, buffer_end = "", None, None

    for start, end, text in paragraphs:
        candidate = f"{buffer} {text}".strip() if buffer else text
        if buffer and len(candidate) > TARGET_CHARS:
            prior_end = buffer_end
            flush()
            overlap = _overlap_tail(chunks[-1][0])
            buffer = f"{overlap} {text}".strip() if overlap else text
            # The overlap is a copy of the tail just flushed, so the new
            # chunk's first character is, physically, still on the page(s)
            # the flushed chunk ended on.
            buffer_start = prior_end
            buffer_end = end
        else:
            if buffer_start is None:
                buffer_start = start
            buffer = candidate
            buffer_end = end
    flush()

    return [
        Chunk(
            chunk_ordinal=ordinal,
            content=content,
            page_from=_page_at(start, page_starts),
            page_to=_page_at(end, page_starts),
        )
        for ordinal, (content, start, end) in enumerate(chunks)
    ]
