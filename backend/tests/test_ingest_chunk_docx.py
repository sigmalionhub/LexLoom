"""What the `.docx` chunker must get right.

The committed fixture (`fixtures/nist_sp_800_63b_chunks.json`) pins the chunk
boundaries, and it is worth being precise about what that proves: it is
generated from this implementation, so it proves **stability, not
correctness** — a wrong-but-deterministic chunker would produce it just as
happily. This repository already paid for that confusion once: the GDPR
chunk-digest test stayed green through a parser that mangled every word.

Correctness is carried by the tests in this file that take their expectations
from the published document instead — the locator assertions below, and
`test_ingest_extract_docx.py`'s verbatim phrases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.ingest.catalogue import CATALOGUE
from app.ingest.chunk_docx import PATH_SEPARATOR
from app.ingest.parse import TARGET_CHARS, Chunk, parse_source

SOURCE = CATALOGUE["nist-sp-800-63b"]
FIXTURE = Path(__file__).parent / "fixtures" / "nist_sp_800_63b_chunks.json"

# Four headings in this document read "Reauthentication": three at level 3, one
# under Session Management at level 2. The fourth is the load-bearing case —
# it sits at a different depth, so an off-by-one walk that drops the leaf still
# gives the three level-3 cases distinct values and would pass a
# mere-distinctness assertion. Verbatim, therefore, not merely distinct.
REAUTHENTICATION_PATHS = {
    "Authentication Assurance Levels"
    + PATH_SEPARATOR
    + "Authentication Assurance Level 1"
    + PATH_SEPARATOR
    + "Reauthentication",
    "Authentication Assurance Levels"
    + PATH_SEPARATOR
    + "Authentication Assurance Level 2"
    + PATH_SEPARATOR
    + "Reauthentication",
    "Authentication Assurance Levels"
    + PATH_SEPARATOR
    + "Authentication Assurance Level 3"
    + PATH_SEPARATOR
    + "Reauthentication",
    "Session Management" + PATH_SEPARATOR + "Reauthentication",
}


@pytest.fixture(scope="module")
def chunks() -> list[Chunk]:
    return parse_source(SOURCE)


def test_the_locator_names_the_section_the_chunk_is_actually_in(chunks: list[Chunk]) -> None:
    """Verbatim paths, all four of them.

    "Last heading seen" collapses all four to `Reauthentication`. An off-by-one
    walk that emits the parent chain without the leaf gives `... › AAL 1`,
    `... › AAL 2`, `... › AAL 3` — three distinct values, and a test that only
    checked distinctness would pass. Only the exact strings catch both.
    """
    found = {c.locator for c in chunks if c.locator and c.locator.endswith("Reauthentication")}
    assert found == REAUTHENTICATION_PATHS


def test_every_chunk_has_a_locator_and_no_pages(chunks: list[Chunk]) -> None:
    """A `.docx` chunk that claimed a page number would be a number pointing at
    nothing; one with no locator would be a citation a reader cannot look up."""
    assert all(c.locator for c in chunks)
    assert all(c.page_from is None and c.page_to is None for c in chunks)


def test_no_chunk_straddles_a_heading(chunks: list[Chunk]) -> None:
    """The rule the locator's honesty rests on, asserted on offsets.

    A locator is a scalar, so "the heading in effect at the chunk's start" is
    only honest if the chunk's *end* is in the same section. This recomputes the
    section of each chunk's first and last character and requires them to agree.

    It is not a formality. Measured on this document with `flush_at` removed:
    132 of 712 chunks straddle a heading — 19% of citations would name a section
    they hold only a fragment of, and `pack.overlap_tail` makes it worse by
    prefixing up to 50 characters of the previous section onto the next chunk.
    With `flush_at`, the number is 0 and every one of the 145 headings begins a
    chunk.
    """
    from bisect import bisect_right

    from app.ingest.chunk_docx import _concatenate
    from app.ingest.extract_docx import extract_docx

    blocks = extract_docx(SOURCE.path)
    _, starts = _concatenate([b.text for b in blocks])
    heading_starts = [s for b, s in zip(blocks, starts, strict=True) if b.heading_level is not None]

    # Re-pack so the offsets are in hand; `chunk_docx` returns content only.
    from app.ingest.pack import pack_chunks

    paragraphs = [(s, s + len(b.text), b.text) for b, s in zip(blocks, starts, strict=True)]
    packed = pack_chunks(paragraphs, flush_at=frozenset(heading_starts))
    assert len(packed) == len(chunks)

    def section_of(offset: int) -> int:
        return bisect_right(heading_starts, offset) - 1

    straddling = [1 for _content, start, end in packed if section_of(start) != section_of(end - 1)]
    assert not straddling
    assert sum(1 for _c, start, _e in packed if start in set(heading_starts)) == len(heading_starts)


def test_the_gdpr_paragraph_rule_did_not_leak_onto_this_path(chunks: list[Chunk]) -> None:
    """`parse.py`'s boundary regex splits on "(a) ", "1. ", "(30) " and
    "Article N " because a PDF gives it no paragraph markup to work from. A
    `.docx` does, and applying that regex here would cut paragraphs apart at
    those markers.

    The victim is selected with **that regex itself**, imported rather than
    retyped, so this test cannot drift away from the rule it is about: the
    longest extracted block under `TARGET_CHARS` that the regex would cut
    somewhere other than its first characters. Measured on this document, 27
    blocks qualify — all via the "1. " alternative; "Article N " and "(30) "
    never occur here, which is itself the point: those two are GDPR
    typography.

    If the regex ran on this path, that block would be split at the marker and
    could not appear intact inside any single chunk.
    """
    from app.ingest.extract_docx import extract_docx
    from app.ingest.parse import _PARAGRAPH_BOUNDARY

    candidates = []
    for block in extract_docx(SOURCE.path):
        if len(block.text) > TARGET_CHARS:
            continue
        match = _PARAGRAPH_BOUNDARY.search(block.text)
        if match is not None and match.start() > 20:
            candidates.append(block.text)
    assert candidates, "no paragraph the regex would cut; re-pick this fixture"
    victim = max(candidates, key=len)
    assert any(victim in c.content for c in chunks)


def test_the_chunking_is_the_one_the_fixture_was_built_against(chunks: list[Chunk]) -> None:
    """Stability, not correctness — see the module docstring.

    It exists so a dependency bump or an accidental edit to `pack.py` announces
    itself instead of silently renumbering every `chunk_ordinal`, which is a
    chunk's identity across every tenant's copy.
    """
    expected = json.loads(FIXTURE.read_text())
    actual = [
        {"ordinal": c.chunk_ordinal, "locator": c.locator, "chars": len(c.content)} for c in chunks
    ]
    assert actual == expected


def test_the_catalogue_expectation_was_measured(chunks: list[Chunk]) -> None:
    """`expected_chunks` is `None` until someone runs a real parse. This is the
    guard that stops a source shipping with an expectation nobody measured — an
    unmeasured expectation gates nothing."""
    assert SOURCE.expected_chunks is not None
    assert len(chunks) == SOURCE.expected_chunks


def test_no_catalogue_entry_ships_without_a_measured_expectation() -> None:
    for key, source in CATALOGUE.items():
        assert source.expected_chunks is not None, f"{key}: expected_chunks was never measured"
