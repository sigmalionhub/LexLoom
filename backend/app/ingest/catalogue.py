"""The fixed catalogue of source documents this demo can ingest.

A literal `dict[str, Source]`, not `os.listdir()` over `sources/`. Reading the
directory would be more flexible and would be a mistake: the set of available
sources would then be defined by the contents of the image, and a `key` taken
straight from a request would become a filesystem path. The literal makes
`key not in CATALOGUE` the only possible outcome for any input that is not
one of the documents named here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_SOURCES = Path(__file__).resolve().parent / "sources"


@dataclass(frozen=True)
class Source:
    key: str
    title: str
    path: Path
    # Which extractor `parse_source` dispatches to. A media type rather than a
    # file extension: the extension is an artefact of how the file was named,
    # the media type is a statement about what is inside it, and `parse.py`
    # branches on the latter.
    media_type: str
    # `None` for a format that has no pages. `document_chunks.page_from` /
    # `page_to` are nullable from migration 0009 for the same reason: a `.docx`
    # chunk that claimed a page number would be a number pointing at nothing.
    # What it carries instead is `locator` — the heading path.
    pages: int | None
    # Provenance travels with the entry, not in a README. `chat/corpus.py`
    # already made this choice for the seed corpus ("the provenance ... travels
    # in the file itself rather than here, so it cannot drift away from the
    # data it describes"); a binary blob in the tree needs it more, not less,
    # because nothing about a PDF says where it came from or who may
    # redistribute it.
    source_url: str
    retrieved_at: str  # ISO date; a PDF has no version, so the date is the version
    licence: str
    # The digest is not paranoia about tampering. It is what makes the M2.4
    # gold set meaningful: the set keys on (source_key, chunk_ordinal), so a
    # swapped or re-downloaded PDF shifts every ordinal and turns every metric
    # into a number about a different document, silently and with no test
    # failing anywhere else. `test_ingest_parse.py` checks the file on disk
    # against this value on every run.
    sha256: str
    # Empty when the publisher's own file is committed unchanged, as for the
    # GDPR PDF. Non-empty means the committed file is OURS: it names the command
    # that produced it, so anyone can re-run it and compare both digests. A
    # converted artefact with no such statement is the whole class of problem
    # this repository is a case study about.
    conversion: str | None = None
    # `.docx` only. A zip records a modification time per entry, so two runs
    # over identical input can differ in the file digest while the content is
    # identical. `build_docx_source.sh` rezips with fixed timestamps, which
    # makes BOTH digests reproducible — this field exists so the claim can be
    # checked against the part that carries the text, not just the container.
    document_xml_sha256: str | None = None
    # Measured against the committed file by `parse_source()`, never guessed.
    # Last field and defaulted so a source can be committed before its chunking
    # exists — but `test_ingest_catalogue.py` refuses to let a `None` survive
    # into a release, because an unmeasured expectation gates nothing.
    expected_chunks: int | None = None


CATALOGUE: dict[str, Source] = {
    "gdpr-full": Source(
        key="gdpr-full",
        title=(
            "Regulation (EU) 2016/679 of the European Parliament and of the "
            "Council of 27 April 2016 on the protection of natural persons with "
            "regard to the processing of personal data and on the free "
            "movement of such data, and repealing Directive 95/46/EC (General "
            "Data Protection Regulation)"
        ),
        path=_SOURCES / "gdpr-full.pdf",
        media_type="application/pdf",
        pages=88,
        # Measured against the committed file by parse_source(), not guessed:
        # see task-22-report.md for the run that produced this number.
        #
        # Changed from 1027 to 978 in "Fix round 1" (pypdf -> pdfminer.six,
        # after the eval task found pypdf broke words apart on nearly every
        # line). An earlier version of this comment attributed the change to
        # the `Article\s\d+\s` paragraph-boundary rule going from dead code
        # (pypdf never left "Article" intact) to firing as designed. That
        # explanation was wrong and was corrected after review: ablating that
        # one alternative from the boundary regex and re-packing the same
        # (clean) text moves the count from 978 to 980 — the rule accounts for
        # 2 chunks, not 49. The real driver is that pypdf's word-splitting
        # inflated the extracted text by 13,863 characters (369,860 ->
        # 355,997) purely as spurious spaces; divided by this document's
        # measured ~364 characters of new content per chunk, that alone
        # predicts ~38 of the 49-chunk difference. See task-22-report.md,
        # "Fix round 2", for the full measurement.
        expected_chunks=978,
        # The Publications Office's own PDF export for this CELEX number, the
        # same 88-page OJ L 119 typesetting `seed/gdpr_excerpts.json` was taken
        # from (`content_format.extraction` in that file). Fetched, not the
        # EUR-Lex "consolidated text" ELI alias: the consolidated document is
        # regenerated over time as corrigenda land and does not carry this
        # fixed pagination, which would silently move every page number and
        # every ordinal this catalogue promises to hold still.
        source_url="https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=CELEX:32016R0679",
        retrieved_at="2026-08-04",
        licence=(
            "EUR-Lex, Commission Decision 2011/833/EU: reuse permitted with "
            "acknowledgement of source. The text of EU legislation carries no "
            "reuse restriction; only the EUR-Lex typesetting does, and "
            "2011/833/EU covers it. Same terms `seed/gdpr_excerpts.json` was "
            "taken under."
        ),
        sha256="bd84e63f5b622b739a83389afc3b30d240f792bb88d8eb03a816c9a82b0c2499",
    ),
    "nist-sp-800-63b": Source(
        key="nist-sp-800-63b",
        title=(
            "NIST Special Publication 800-63B rev 4, Digital Identity "
            "Guidelines: Authentication and Authenticator Management"
        ),
        path=_SOURCES / "nist-sp-800-63b.docx",
        media_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        # No pages. NIST publishes this document as HTML and PDF; the committed
        # artefact is built from the HTML, which has no pagination to carry. The
        # citation shows `locator` — the heading path — instead, and that is a
        # position a reader can actually find with Ctrl-F.
        pages=None,
        source_url="https://pages.nist.gov/800-63-4/sp800-63b.html",
        retrieved_at="2026-08-05",
        # Quoted verbatim from https://www.nist.gov/oism/copyrights, fetched on
        # the date above. The document itself says nothing about rights — zero
        # matches for "copyright", "public domain" or "17 U.S.C" anywhere in the
        # published HTML, which is why the licence is cited from the publisher's
        # own policy page rather than from the file.
        licence=(
            'NIST, https://www.nist.gov/oism/copyrights: "NIST web pages are '
            "provided as a public service by the National Institute of "
            "Standards and Technology (NIST). With the exception of material "
            "marked as copyrighted, information presented on NIST sites are "
            "considered public information and may be distributed or copied. "
            'Use of appropriate byline/photo/image credits is requested." '
            "Nothing in this document is marked as copyrighted. Separately, and "
            "as a conclusion rather than a quotation: a work of the United "
            "States Government is not subject to copyright protection in the "
            "United States (17 U.S.C. 105)."
        ),
        # Both digests are of OUR artefact, and both are reproducible: two runs
        # of the script over the same fetched HTML produced byte-identical
        # output, verified. An earlier draft of this work claimed the file
        # digest could not be reproducible because a zip stores per-entry
        # mtimes; that was true of the naive build and false of this one, which
        # normalises them.
        sha256="391aa0a370b2f4884821a2e6ba2da19e1bcc67f563560eee6f355ae588760408",
        document_xml_sha256=("9ca7af97efd756c4c053219943a1e9335adcd07eedee9a6cf64ebcfe98ecf9f9"),
        conversion=(
            "Built by backend/scripts/build_docx_source.sh with LibreOffice "
            "24.2.7.2 420(Build:2) on 2026-08-05: the publisher's HTML is "
            "stripped of one <nav>, one <footer>, the page-switch div and the "
            "page build timestamp, its 616 anchors are unwrapped and its images "
            "and scripts removed, then converted and the package sanitised. The "
            "bibliography is deliberately kept: it sits mid-document, ahead of "
            "appendices A-E, and removing it would leave dangling references "
            "and make this title an overstatement. The preprocessed HTML is "
            "committed beside the .docx as nist-sp-800-63b.src.html so every "
            "test that needs ground truth can quote the publisher rather than "
            "this repository's own extractor."
        ),
        # Deliberately unset. The number has to come from a real parse, and the
        # chunker does not exist yet; a plausible placeholder here would be a
        # gate that passes without measuring anything, which is the failure this
        # catalogue's other comments already document. The task that measures it
        # also adds the guard to `test_ingest_catalogue.py` that refuses a None.
        expected_chunks=None,
    ),
}
