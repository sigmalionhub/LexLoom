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
    pages: int
    expected_chunks: int
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
}
