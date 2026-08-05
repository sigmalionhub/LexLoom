"""Text out of a `.docx`, in the order a reader would meet it.

**Why a `.docx` sits in this catalogue at all.** The PDF path
(`app/ingest/parse.py`) has to *infer* where one paragraph ends and the next
begins, because PDF extraction returns running text with no paragraph markup
surviving — hence the `_PARAGRAPH_BOUNDARY` regex there, written against the
GDPR's specific typography ("Article 4 ", "(30) ", "1. ", "(a) "). A `.docx`
does not need any of that: the format states which paragraphs are headings and
at what level, so the structure is *given*. That is the difference this second
source exists to show, and it is why `chunk_docx.py` never imports that regex.

**Why this walks the XML instead of using `document.paragraphs`.**
`python-docx`'s `Document.paragraphs` yields only the body's top-level `<w:p>`
elements. Every paragraph inside a table is absent from it — measured on this
catalogue's `.docx`: the body holds 5 tables, and their paragraphs never appear
in that list. The tempting fix, `document.paragraphs` followed by
`document.tables`, extracts every word and puts all five tables at the end of
the document, which is silently wrong in exactly the way a count assertion
cannot see: the chunker downstream files each chunk under the heading in effect
at its start, so relocated tables get filed under the wrong section.

`python-docx` exposes no public iterator over the body in document order, so
this walks `document.element.body` and constructs `Paragraph` and `Table`
itself. That is a documented cost of the library, not a workaround for a bug.

**Styles are raw `w:styleId` values, not display names.** `styleId` is
`Heading1` and `TableContents`; `paragraph.style.name` is `Heading 1` and
`Table Contents`. Both appear in the wild and confusing them is a real hazard:
matching the substring "Heading" against display names also selects
`ListHeading` and `TableHeading`, which in this document is 187 paragraphs that
are not headings. Heading level therefore comes from an exact match on the
`styleId`, never from a substring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

# `Heading1`..`Heading9` as a styleId; the exactness is the point. Word also
# writes `Heading1Char` for the character style of the same name, which is not a
# paragraph style and must not match.
_HEADING_STYLE = re.compile(r"^Heading([1-9])$")

# Deeper levels exist in the OOXML spec but carry no structure worth a locator
# in this catalogue's documents, and admitting them would make the heading path
# longer without making it more precise.
_MAX_HEADING_LEVEL = 4

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Block:
    """One paragraph of the document, wherever it lives.

    A table cell's paragraph is a `Block` like any other: the chunker treats the
    document as a stream of paragraphs and has no reason to care which of them
    came out of a table, only that they arrive in the right order.
    """

    text: str
    #: Raw `w:styleId` — `BodyText`, `Heading2`, `TableContents`. Never a
    #: display name; see the module docstring.
    style: str
    #: 1..4 for a heading, `None` for everything else.
    heading_level: int | None


def _heading_level(style_id: str) -> int | None:
    match = _HEADING_STYLE.match(style_id)
    if match is None:
        return None
    level = int(match.group(1))
    return level if level <= _MAX_HEADING_LEVEL else None


def _normalise(text: str) -> str:
    """Collapse runs of whitespace, including the newlines a `<w:br/>` becomes.

    The same normalisation `parse.py` applies to PDF text, and for the same
    reason: a line break inside a sentence is a fact about typesetting, not
    about the sentence, and leaving it in would put it inside an FTS token.
    """
    return _WHITESPACE.sub(" ", text).strip()


def _paragraph_block(paragraph: Paragraph) -> Block | None:
    text = _normalise(paragraph.text)
    if not text:
        return None
    # `style_id` is None for a paragraph carrying no explicit style; the
    # document's default applies, and for this catalogue that is body text.
    style_id = paragraph.style.style_id if paragraph.style is not None else None
    style = style_id or "Normal"
    return Block(text=text, style=style, heading_level=_heading_level(style))


def _table_blocks(table: Table) -> list[Block]:
    """A table's paragraphs, row by row and cell by cell.

    Reading order for a table is genuinely ambiguous, and this picks the one the
    document itself implies: rows top to bottom, cells left to right, which is
    how the published HTML reads and how a person reads it aloud. Nested tables
    recurse, because a cell holds a body of its own.
    """
    blocks: list[Block] = []
    for row in table.rows:
        for cell in row.cells:
            for child in cell._element.iterchildren():
                if child.tag == qn("w:p"):
                    block = _paragraph_block(Paragraph(child, cell))
                    if block is not None:
                        blocks.append(block)
                elif child.tag == qn("w:tbl"):
                    blocks.extend(_table_blocks(Table(child, cell)))
    return blocks


def extract_docx(path: Path) -> list[Block]:
    """Every non-empty paragraph of `path`, in document order.

    Deterministic by construction: the same file yields the same list, because
    nothing here depends on anything but the bytes. That property is load-bearing
    — `(source_key, chunk_ordinal)` is a chunk's identity across every tenant's
    copy — but it is not sufficient on its own, which is why
    `test_ingest_extract_docx.py` takes its expectations from the publisher's
    own HTML rather than from this function's output.
    """
    document = Document(str(path))
    blocks: list[Block] = []
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            block = _paragraph_block(Paragraph(child, document))
            if block is not None:
                blocks.append(block)
        elif child.tag == qn("w:tbl"):
            blocks.extend(_table_blocks(Table(child, document)))
    return blocks
