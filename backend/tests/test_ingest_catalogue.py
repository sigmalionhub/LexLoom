"""What the committed source files are, checked rather than described.

`test_ingest_parse.py` already pins the GDPR PDF's digest, because the gold set
keys on `(source_key, chunk_ordinal)` and a swapped file shifts every ordinal
silently. These tests generalise that to the catalogue as a whole and add the
checks a *converted* artefact needs and a downloaded one does not.

**What a digest test can and cannot catch.** It catches a later substitution:
someone re-runs the build script, or drops in a different file, and the pin
disagrees. It does *not* catch "the conversion was wrong in the first place",
because the value in the catalogue was read off the same file it now guards.
That is not a flaw to fix here — it is the reason the extraction tests exist and
take their ground truth from the publisher's own HTML rather than from our
extractor's output.
"""

from __future__ import annotations

import hashlib
import zipfile

import pytest

from app.ingest.catalogue import CATALOGUE, Source

DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("source", CATALOGUE.values(), ids=list(CATALOGUE))
def test_the_committed_file_is_the_one_the_catalogue_names(source: Source) -> None:
    assert source.path.exists(), f"{source.key}: {source.path} is missing"
    assert _sha256(source.path.read_bytes()) == source.sha256


@pytest.mark.parametrize("source", CATALOGUE.values(), ids=list(CATALOGUE))
def test_provenance_is_present_and_says_whose_artefact_this_is(source: Source) -> None:
    """A converted file must say so; an unconverted one must not pretend it was.

    `conversion` is the difference between "we downloaded this" and "we built
    this", and a reader who cannot tell them apart cannot check either.
    """
    assert source.source_url.startswith("https://")
    assert source.retrieved_at
    assert source.licence
    if source.media_type == DOCX_MEDIA_TYPE:
        assert source.conversion, f"{source.key}: a built artefact must name its build"
        assert "build_docx_source.sh" in source.conversion
    else:
        assert source.conversion is None


@pytest.mark.parametrize(
    "source",
    [s for s in CATALOGUE.values() if s.media_type == DOCX_MEDIA_TYPE],
    ids=[k for k, s in CATALOGUE.items() if s.media_type == DOCX_MEDIA_TYPE],
)
def test_the_docx_carries_the_text_digest_it_claims(source: Source) -> None:
    """A zip stores an mtime per entry, so the file digest can drift while the
    text is identical. `build_docx_source.sh` normalises those timestamps, which
    makes both digests reproducible — this test guards the one that carries the
    words, so a rebuild that changed the *content* cannot hide behind a matching
    container or vice versa.
    """
    with zipfile.ZipFile(source.path) as package:
        assert _sha256(package.read("word/document.xml")) == source.document_xml_sha256


@pytest.mark.parametrize(
    "source",
    [s for s in CATALOGUE.values() if s.media_type == DOCX_MEDIA_TYPE],
    ids=[k for k, s in CATALOGUE.items() if s.media_type == DOCX_MEDIA_TYPE],
)
def test_the_docx_package_carries_nothing_but_the_document(source: Source) -> None:
    """The sanitise stage of the build script, asserted over the committed file.

    A one-time inventory recorded in a report disappears the moment anyone
    rebuilds. The raw LibreOffice conversion carried 67 external relationships
    (three of them dead `file:///` image targets) and a `word/comments.xml`
    holding the scraped page `<head>`; none of it is reachable by `python-docx`,
    so this is not a runtime control. It is a promise about what a prospective
    client finds when they open the file we committed.
    """
    forbidden = (
        b"vbaProject",
        b"oleObject",
        b"DDEAUTO",
        b"INCLUDETEXT",
        b"<!DOCTYPE",
        b"<!ENTITY",
        b'TargetMode="External"',
    )
    with zipfile.ZipFile(source.path) as package:
        names = package.namelist()
        assert "word/comments.xml" not in names
        assert "docProps/custom.xml" not in names

        total = 0
        for info in package.infolist():
            total += info.file_size
            blob = package.read(info.filename)
            for pattern in forbidden:
                assert pattern not in blob, f"{info.filename} carries {pattern!r}"

        # Not a zip bomb. Measured at 9.4x on the committed file; the bound is
        # loose enough not to be a tripwire for ordinary XML and tight enough to
        # notice an embedded payload.
        compressed = sum(info.compress_size for info in package.infolist())
        assert total / compressed < 30
