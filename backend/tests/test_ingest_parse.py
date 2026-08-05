"""The catalogue and parser branch D adds: a fixed source list, a deterministic
chunker, and the stable (source_key, chunk_ordinal) identity the M2.4 gold set
keys on.
"""

import hashlib
import json
from pathlib import Path

import pytest

from app.ingest.catalogue import CATALOGUE
from app.ingest.parse import OVERLAP_CHARS, TARGET_CHARS, _concatenate, _extract_pages, parse_source

FIXTURE = Path(__file__).parent / "fixtures" / "gdpr-full.chunks.json"

# Single-letter-token rate, first 20 pages, measured during extractor
# selection (task-22-report.md, "Fix round 1"; classification numbers in "Fix
# round 2"):
#   pypdf, default extraction        9.3% (1815/19569)
#   pypdf, extraction_mode="layout"  2.8% ( 462/16686)
#   pdftotext (poppler)              2.1% ( 340/15905)
#   pdfminer.six (chosen)            2.1% ( 340/15908)
# 5% is a gross-breakage guard, and this comment states exactly what it does
# and does not catch — it previously claimed headroom "below every broken
# measurement (>=2.8x pypdf's best mode)", which was false twice over: 5% is
# ABOVE pypdf-layout's 2.8%, not below it, and the "2.8x" was the table's 2.8%
# restated as if it were a ratio.
#
# What 5% actually buys, re-derived from the table above:
#   caught   pypdf default, 9.3% — 1.86x over the ceiling.
#   NOT caught  pypdf layout, 2.8%. A regression to that extractor passes.
#   headroom  2.02x over the 2.48% this test measures across the whole
#             document, and 2.38x over pdfminer's own 2.1% on the 20-page
#             sample. (The whole-document rate runs a little higher than the
#             sample because of legitimate single-letter tokens like "a" and
#             "I" that a 20-page sample under-represents.)
#
# The gap to pypdf-layout is left open on purpose rather than closed by
# tightening. Closing it needs a ceiling under 2.8%, which leaves at most
# 0.32 percentage points above the 2.48% this test measures — an 11% margin on
# a figure that moves with the corpus. That is a guard that goes red on a
# legitimate document change, and a guard that cries wolf gets its threshold
# raised by whoever is unblocking CI, which loses the 9.3% catch as well.
# Extractor identity is not a thing a token-rate threshold can assert; if a
# regression to pypdf-layout needs catching, assert the extractor.
_SINGLE_LETTER_TOKEN_RATE_CEILING = 0.05


@pytest.fixture(scope="module")
def gdpr_full_chunks() -> list:
    """Parsed once per test module, not once per test.

    `parse_source` is a pure function over an immutable committed file,
    returning a list of frozen `Chunk`s that no test below mutates, so
    sharing one parse across every test in this module changes nothing about
    what any test can observe. What it avoids is re-parsing an 88-page PDF
    (~10-11s per parse) up to five extra times in this one file. If a future
    test needs to mutate the result, it must copy it first rather than
    narrowing this fixture's scope back down — a `function`-scoped fixture
    silently reintroduces the cost this one exists to remove.
    """
    return parse_source(CATALOGUE["gdpr-full"])


@pytest.fixture(scope="module")
def gdpr_full_text() -> str:
    """The concatenated, page-joined extraction `parse_source` builds
    internally on its way to chunking — shared the same way, and for the
    same reason, as `gdpr_full_chunks`, and kept as a separate fixture
    because the two readability tests below need the raw extracted text, not
    the packed chunks.
    """
    pages = _extract_pages(CATALOGUE["gdpr-full"])
    full_text, _ = _concatenate(pages)
    return full_text


def _digest_rows(chunks) -> list[list]:
    """(chunk_ordinal, sha256(content), page_from, page_to) for every chunk.

    Digests rather than the text itself: the fixture stays a few dozen KB
    instead of holding the whole document a second time, and a whitespace
    change in extraction is still a different digest.
    """
    return [
        [
            chunk.chunk_ordinal,
            hashlib.sha256(chunk.content.encode()).hexdigest(),
            chunk.page_from,
            chunk.page_to,
        ]
        for chunk in chunks
    ]


def test_the_catalogued_pdf_is_the_file_the_digest_names() -> None:
    """One line, and it catches exactly the case where the gold set silently
    starts measuring a different document: a swapped or re-downloaded PDF."""
    source = CATALOGUE["gdpr-full"]
    assert hashlib.sha256(source.path.read_bytes()).hexdigest() == source.sha256


def test_the_chunking_is_the_one_the_gold_set_was_built_against(gdpr_full_chunks) -> None:
    """Pin the chunk boundaries as data, because the gold set is data about them.

    Re-running the chunker twice in one process would prove nothing — a pure
    function is deterministic whether it is correct or not; a broken chunker
    is exactly as deterministic as a working one. What can actually change
    underneath this is the chunk size, the overlap, the paragraph rule, or
    pdfminer.six's extraction across a version bump (it replaced pypdf in fix
    round 1 for exactly this reason: extraction quality is data here, not an
    API contract). A committed fixture is the only thing that sees all four,
    and it sees them at the moment they change rather than in M2.4 when the
    eval numbers quietly move to describe a different document.

    This test proves stability, not readability: digests pass identically
    whether the underlying text is clean prose or the extractor's garbage, so
    it would have passed throughout this task's life against pypdf's broken
    output just as happily as it passes now. See
    test_known_damaged_phrases_survive_extraction_intact and
    test_single_letter_token_rate_stays_low below for the property this one
    cannot check.
    """
    source = CATALOGUE["gdpr-full"]
    assert hashlib.sha256(source.path.read_bytes()).hexdigest() == source.sha256

    assert len(gdpr_full_chunks) == source.expected_chunks

    fixture = json.loads(FIXTURE.read_text())
    assert _digest_rows(gdpr_full_chunks) == fixture["gdpr-full"]


def test_chunk_ordinals_are_sequential_from_zero(gdpr_full_chunks) -> None:
    """The pair the gold set keys on is (source_key, chunk_ordinal); ordinal
    is the chunk's position in this list, so it must have no gaps and no
    repeats."""
    assert [chunk.chunk_ordinal for chunk in gdpr_full_chunks] == list(range(len(gdpr_full_chunks)))


def test_every_chunk_has_a_page_range_inside_the_source(gdpr_full_chunks) -> None:
    source = CATALOGUE["gdpr-full"]
    assert gdpr_full_chunks, "the parser produced no chunks at all"
    for chunk in gdpr_full_chunks:
        assert 1 <= chunk.page_from <= chunk.page_to <= source.pages
        assert chunk.content, f"chunk {chunk.chunk_ordinal} is empty"


def test_pages_advance_monotonically_across_the_document(gdpr_full_chunks) -> None:
    """Page numbers never run backwards from one chunk to the next — the
    parser reads the document once, front to back."""
    last_page = 0
    for chunk in gdpr_full_chunks:
        assert chunk.page_from >= last_page
        last_page = chunk.page_to


def test_chunks_stay_near_the_size_contract(gdpr_full_chunks) -> None:
    """~500 characters, ~50 overlap — the shape the citation panel already
    assumes for a citation card (`frontend/components/sources.tsx`). Not an
    exact bound: the paragraph rule and the hard-split fallback both round
    outward a little, so the check is against a generous margin rather than
    the constants themselves, which would make this test change every time
    the constants do for no reason connected to what it is checking."""
    for chunk in gdpr_full_chunks:
        assert len(chunk.content) <= TARGET_CHARS + OVERLAP_CHARS


def test_known_damaged_phrases_survive_extraction_intact(gdpr_full_text) -> None:
    """The bug this task shipped and had to fix, made unable to come back.

    `test_the_chunking_is_the_one_the_gold_set_was_built_against` pins
    digests derived *from the extractor's own output*, so a chunker built on
    top of a broken extractor is exactly as stable — and exactly as green —
    as one built on a working one; that test alone passed against pypdf's
    output for this task's entire first two rounds. These two phrases are the
    ones pypdf broke into "Ha ving regar d to the opinion" and "Af ter
    transmission of the draf t legislative act" (task-22-report.md, "Fix
    round 1"); checking for the readable form directly is the one thing the
    digest fixture cannot do, because a digest of garbage is just as stable a
    digest as one of prose.
    """
    damaged_phrases = (
        "Having regard to the opinion",
        "After transmission of the draft legislative act",
    )
    for phrase in damaged_phrases:
        assert phrase in gdpr_full_text, f"{phrase!r} did not survive extraction intact"


def test_single_letter_token_rate_stays_low(gdpr_full_text) -> None:
    """A broken extractor breaks words into single-letter fragments across
    the whole document, not just in the two phrases the other test checks by
    name — this is the aggregate version of the same property. See the
    module-level comment on `_SINGLE_LETTER_TOKEN_RATE_CEILING` for the four
    measured rates the threshold was chosen against.
    """
    tokens = gdpr_full_text.split()
    single_letter = sum(1 for token in tokens if len(token) == 1)
    rate = single_letter / len(tokens)
    assert rate < _SINGLE_LETTER_TOKEN_RATE_CEILING, (
        f"{single_letter}/{len(tokens)} = {rate:.1%} of tokens are a single letter, "
        f"at or above the {_SINGLE_LETTER_TOKEN_RATE_CEILING:.0%} ceiling — the extractor "
        f"is likely breaking words apart again"
    )
