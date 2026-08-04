"""The catalogue and parser branch D adds: a fixed source list, a deterministic
chunker, and the stable (source_key, chunk_ordinal) identity the M2.4 gold set
keys on.
"""

import hashlib
import json
from pathlib import Path

from app.ingest.catalogue import CATALOGUE
from app.ingest.parse import OVERLAP_CHARS, TARGET_CHARS, parse_source

FIXTURE = Path(__file__).parent / "fixtures" / "gdpr-full.chunks.json"


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


def test_the_chunking_is_the_one_the_gold_set_was_built_against() -> None:
    """Pin the chunk boundaries as data, because the gold set is data about them.

    Re-running the chunker twice in one process would prove nothing — a pure
    function is deterministic whether it is correct or not; a broken chunker
    is exactly as deterministic as a working one. What can actually change
    underneath this is the chunk size, the overlap, the paragraph rule, or
    pypdf's extraction across a version bump. A committed fixture is the only
    thing that sees all four, and it sees them at the moment they change
    rather than in M2.4 when the eval numbers quietly move to describe a
    different document.
    """
    source = CATALOGUE["gdpr-full"]
    assert hashlib.sha256(source.path.read_bytes()).hexdigest() == source.sha256

    chunks = parse_source(source)
    assert len(chunks) == source.expected_chunks

    fixture = json.loads(FIXTURE.read_text())
    assert _digest_rows(chunks) == fixture["gdpr-full"]


def test_chunk_ordinals_are_sequential_from_zero() -> None:
    """The pair the gold set keys on is (source_key, chunk_ordinal); ordinal
    is the chunk's position in this list, so it must have no gaps and no
    repeats."""
    chunks = parse_source(CATALOGUE["gdpr-full"])
    assert [chunk.chunk_ordinal for chunk in chunks] == list(range(len(chunks)))


def test_every_chunk_has_a_page_range_inside_the_source() -> None:
    source = CATALOGUE["gdpr-full"]
    chunks = parse_source(source)
    assert chunks, "the parser produced no chunks at all"
    for chunk in chunks:
        assert 1 <= chunk.page_from <= chunk.page_to <= source.pages
        assert chunk.content, f"chunk {chunk.chunk_ordinal} is empty"


def test_pages_advance_monotonically_across_the_document() -> None:
    """Page numbers never run backwards from one chunk to the next — the
    parser reads the document once, front to back."""
    chunks = parse_source(CATALOGUE["gdpr-full"])
    last_page = 0
    for chunk in chunks:
        assert chunk.page_from >= last_page
        last_page = chunk.page_to


def test_chunks_stay_near_the_size_contract() -> None:
    """~500 characters, ~50 overlap — the shape the citation panel already
    assumes for a citation card (`frontend/components/sources.tsx`). Not an
    exact bound: the paragraph rule and the hard-split fallback both round
    outward a little, so the check is against a generous margin rather than
    the constants themselves, which would make this test change every time
    the constants do for no reason connected to what it is checking."""
    chunks = parse_source(CATALOGUE["gdpr-full"])
    for chunk in chunks:
        assert len(chunk.content) <= TARGET_CHARS + OVERLAP_CHARS
