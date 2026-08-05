"""A deterministic PDF parser and chunker.

The parser exists to give every chunk of every catalogue source a stable
identity — `(source_key, chunk_ordinal)` — that survives a re-parse in a
different process, on a different machine, on a different day. That is a
stronger requirement than "the same input gives the same output": a pure
function already has that property whether it is correct or not (see
`test_the_chunking_is_the_one_the_gold_set_was_built_against` in
`tests/test_ingest_parse.py`). What can actually change the output from one
run to the next is the chunk size, the overlap, the paragraph rule, or the
extractor's own output across a version bump — which is exactly why
`pdfminer.six` is pinned exactly (`pyproject.toml`) and why the chunk
boundaries are pinned as data in a committed fixture, not re-derived by a
test that would pass for a broken chunker just as readily as a working one.

Citations already carry `page_from`/`page_to` and the panel already shows
them (`frontend/components/sources.tsx`); this parser keeps that contract by
tracking, for every chunk, which page of the source PDF its first and last
character came from.

**Why `pdfminer.six` and not `pypdf`.** `pypdf` (both its default extraction
and its `extraction_mode="layout"`) breaks this specific PDF's text into
fragments at nearly every glyph run boundary: "Ha ving regar d to the
opinion" for "Having regard to the opinion", never once producing an intact
"Article" across the first 20 pages, with 8-9% of extracted tokens a single
stray letter. That is not a font-rendering quirk visible only in a diff — it
is the difference between an FTS index that tokenises "regard" as one word
and one that tokenises it as "regar" and "d", and between an embedding
computed over readable sentences and one computed over noise wearing the
shape of sentences. `pdfminer.six` (`LAParams(boxes_flow=None)` — see
`_LAPARAMS` below) matches `pdftotext`/poppler measured on the same 20 pages:
7 intact "Article"s (pypdf: 0) and a 2.1% single-letter-token rate (pypdf:
8-9%). See `task-22-report.md`, "Fix round 1", for the full measurement
including the two other candidates tried.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass

from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTTextContainer

from app.ingest.catalogue import Source
from app.ingest.pack import OVERLAP_CHARS, TARGET_CHARS, pack_chunks

# Re-exported, not redefined: `pack.py` owns both numbers now, and
# `tests/test_ingest_parse.py` imports them from here. That test pins the GDPR
# chunk boundaries the gold set is built on, so the fewer things that move
# underneath it during this refactor, the clearer its signal if it breaks.
__all__ = ["OVERLAP_CHARS", "TARGET_CHARS", "Chunk", "parse_source"]

PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_WHITESPACE = re.compile(r"\s+")

# `boxes_flow=None` turns off pdfminer's column-flow heuristic, which — on
# this PDF's hanging-indent recital numbering (the "(1)" sits in its own
# narrow left column, the body text in a wider column to its right) — grouped
# every page's markers together ("(1) (2) The protection ...") ahead of every
# body paragraph instead of interleaving marker-then-body-then-marker in
# reading order. `None` falls back to pdfminer's simpler top-to-bottom
# ordering, which gets this document's actual reading order right: verified
# against `pdftotext`'s output for the same page, which agrees. The default
# (`boxes_flow=0.5`) was tried first and reproduces the misordering.
_LAPARAMS = LAParams(boxes_flow=None)

# What counts as "a paragraph boundary" in this text. This PDF carries no
# blank line between paragraphs in extracted form — the typesetting is fully
# justified running text with no paragraph-level markup surviving extraction
# — so a boundary is defined the way the document itself marks one: the start
# of a numbered recital ("(30) "), an article header ("Article 4 "), a
# numbered paragraph within an article ("1. "), or a lettered sub-point
# ("(a) ").
#
# Footnote and cross-reference markers like "(2)," or "Article 22(1) and (4)"
# mostly do not match, because the closing parenthesis is mostly followed by
# punctuation rather than by whitespace-then-word — but "mostly" is measured,
# not absolute: of the 308 "(N) " matches in the production text, 15 sit
# directly between two lowercase words with nothing but a space either side
# ("...of the Council (4) seeks to harmonise...", recital 3; "...of the
# Council (2) applies to the processing...", recital 17; several more in
# cross-references like "Article 22(1) and (4) and, at least..."). Each of
# those 15 forces a spurious mid-sentence paragraph split. Left unfiltered,
# deliberately: an earlier draft of this comment claimed the false positive
# rate was zero, and it was reviewed as one of the two remaining defects in
# this task (see task-22-report.md, "Fix round 2"). Measured rather than
# reasoned about before deciding not to fix it — ablating exactly these 15
# matches and re-packing leaves the chunk count at 978, unchanged. The
# packer absorbs every one of them by re-merging the fragments back up
# toward the 500-char target, so a real fix would buy a slightly tidier
# paragraph list and no different output, at the cost of the variable-length
# lookbehind it would need.
#
# That zero is itself twice-measured, and it had to be: this comment first
# claimed the false-positive rate was zero (wrong), then claimed the ablation
# moved 978 to 979 (also wrong, by one). Two independent re-packs — the
# scoped re-review's and the controller's — both return 978. Reproduce with
# `_paragraphs` monkeypatched to drop these 15 offsets; a delta of anything
# but zero means the packer's merge behaviour has changed and this paragraph
# is stale.
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
    # `None` for a source whose format has no pages. A `.docx` chunk that
    # claimed a page number would be a number pointing at nothing — see
    # migration 0009, which makes the columns nullable for the same reason.
    page_from: int | None
    page_to: int | None
    # Where in the document this chunk sits, in words a reader can look up.
    # `None` for a paginated source, which says it with page numbers instead.
    # Defaulted and last so every existing construction site keeps working.
    locator: str | None = None


def _extract_pages(source: Source) -> list[str]:
    """One normalised string per page: internal whitespace collapsed to single
    spaces, so page text can be concatenated and searched without a stray
    newline splitting a word the extractor itself kept on one line.

    `extract_pages` + walking each page's `LTTextContainer`s, not
    `pdfminer.high_level.extract_text` per page: the latter reopens and
    reparses the whole file once per page (88 parses for an 88-page
    document), measured at 17.6s for this file; a single pass over
    `extract_pages`, reading off page boundaries as they come, measured at
    10.1s for the same file and the same `LAParams`.
    """
    pages = []
    for page_layout in extract_pages(str(source.path), laparams=_LAPARAMS):
        parts = [
            element.get_text() for element in page_layout if isinstance(element, LTTextContainer)
        ]
        pages.append(_WHITESPACE.sub(" ", "".join(parts)).strip())
    return pages


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


def _parse_pdf(source: Source) -> list[Chunk]:
    """Parse a PDF into chunks with page-anchored, ordinal identity.

    Deterministic by construction: no randomness, no set iteration, no
    wall-clock or filesystem-order dependency — the only inputs are the PDF
    bytes and `pack.py`'s two constants. `chunk_ordinal` is the chunk's index in
    the returned list, in document reading order, which is what
    `test_the_chunking_is_the_one_the_gold_set_was_built_against` pins against a
    committed fixture.

    No `flush_at`: a PDF chunk carries a page *range*, so it is allowed to
    straddle a boundary and say so. The `.docx` path carries a single locator
    and cannot.
    """
    pages = _extract_pages(source)
    full_text, page_starts = _concatenate(pages)
    chunks = pack_chunks(_paragraphs(full_text))

    return [
        Chunk(
            chunk_ordinal=ordinal,
            content=content,
            page_from=_page_at(start, page_starts),
            page_to=_page_at(end, page_starts),
        )
        for ordinal, (content, start, end) in enumerate(chunks)
    ]


def parse_source(source: Source) -> list[Chunk]:
    """Parse `source.path` into chunks, by whichever route its format needs.

    Dispatch is on `source.media_type` rather than on the file extension: the
    extension records how someone named the file, the media type records what is
    inside it, and the catalogue is where that is decided once.
    """
    if source.media_type == PDF_MEDIA_TYPE:
        return _parse_pdf(source)
    if source.media_type == DOCX_MEDIA_TYPE:
        from app.ingest.chunk_docx import chunk_docx

        return chunk_docx(source)
    raise ValueError(f"{source.key}: no parser for media type {source.media_type!r}")
