"""Chunks out of a `.docx`, each labelled with the section it is actually in.

The PDF path anchors a chunk with a page range, because a page is what a reader
of that document has. A `.docx` built from a web publication has no pages, so
this anchors a chunk with its **heading path** — `Authentication Assurance
Levels › Authentication Assurance Level 2 › Reauthentication` — which is a
position a reader can find with Ctrl-F and which the format hands us directly.

**Why the path and not a section number.** NIST's own sub-section numbering
("Sec. 3.1.1", which the body of this document references 152 times) exists
nowhere in the published HTML as data: `data-section` carries only the
top-level chapter, so all three "Reauthentication" headings under AAL1, AAL2 and
AAL3 share the value `2`, and the page's `<nav>` carries 13 chapter markers and
nothing finer. Reconstructing the numbering would mean *inferring* it by walking
and counting, and a wrong section number is worse than none — a reader who
looks up "3.1.1" and finds something else has been misled by a citation, which
is the one thing a citation may never do. The heading path needs no inference:
levels are given by the format, and all 145 paths in this document are distinct.

**Why the locator is a field and not a prefix on the text.** Putting the path
inside `content` was tried on paper and measured: the paths are a median of 88
characters and up to 166, against the 400-character truncation the citation
panel applies — 22% of every excerpt, 42% at worst — and, because `ts_rank` here
is unnormalised and half of this corpus's adjacent score pairs are already
byte-identical, a constant per-section prefix adds a constant to every chunk of
that section and makes ties *more* common rather than less. `content` stays
verbatim document text.

**Why chunks never straddle a heading.** A locator is a scalar, so the rule
"the heading in effect at the chunk's start" has to actually hold. It would not
by default: `pack.overlap_tail` prefixes up to 50 characters of the previous
section onto the next chunk, so a chunk whose start offset lands inside that
tail would be labelled with the section it holds 50 characters of instead of the
one it holds 450 of. `pack_chunks(..., flush_at=...)` closes the buffer at every
heading, which costs a slightly short chunk at each boundary and buys a locator
that is right by construction.
"""

from __future__ import annotations

from bisect import bisect_right
from typing import TYPE_CHECKING

from app.ingest.catalogue import Source
from app.ingest.extract_docx import extract_docx
from app.ingest.pack import pack_chunks

if TYPE_CHECKING:  # pragma: no cover - `parse` imports this module at runtime
    from app.ingest.parse import Chunk

#: Between levels of the heading path. U+203A, chosen because it appears in
#: none of this document's 145 headings — a separator that can occur inside a
#: heading makes the path ambiguous to anything that later tries to split it.
PATH_SEPARATOR = " › "


def _concatenate(texts: list[str]) -> tuple[str, list[int]]:
    """Join blocks with a single space and return each block's start offset.

    The same shape as `parse.py`'s page concatenation, and for the same reason:
    the packer works in offsets, so whatever produced the paragraphs has to be
    able to map an offset back to what it came from.
    """
    parts: list[str] = []
    starts: list[int] = []
    offset = 0
    for text in texts:
        starts.append(offset)
        parts.append(text)
        offset += len(text) + 1
    return " ".join(parts), starts


def chunk_docx(source: Source) -> list[Chunk]:
    """Parse `source.path` into chunks with heading-anchored, ordinal identity.

    Deterministic: the only inputs are the file's bytes and `pack.py`'s
    constants. `chunk_ordinal` is the chunk's index in the returned list, in
    document reading order.
    """
    # Imported here rather than at module scope: `parse.py` imports this module
    # inside `parse_source` to dispatch, and importing `Chunk` from it at module
    # scope would close the cycle.
    from app.ingest.parse import Chunk

    blocks = extract_docx(source.path)
    full_text, block_starts = _concatenate([block.text for block in blocks])

    # The heading path in effect from each heading's offset onwards. Built by
    # walking a stack: a heading at level N replaces the entry for N and drops
    # everything deeper, which is exactly what "the section this text is in"
    # means. Levels may be skipped by a document; this handles that by keying on
    # the level rather than on stack depth.
    stack: dict[int, str] = {}
    heading_starts: list[int] = []
    heading_paths: list[str] = []
    for block, start in zip(blocks, block_starts, strict=True):
        if block.heading_level is None:
            continue
        stack = {level: text for level, text in stack.items() if level < block.heading_level}
        stack[block.heading_level] = block.text
        heading_starts.append(start)
        heading_paths.append(PATH_SEPARATOR.join(stack[level] for level in sorted(stack)))

    # `build_docx_source.sh` asserts that nothing precedes the first heading, and
    # `test_ingest_extract_docx.py` asserts it again on the committed artefact.
    # If both were ever wrong at once, a chunk ahead of the first heading would
    # have no path to take; say so rather than invent one.
    def locator_at(offset: int) -> str | None:
        index = bisect_right(heading_starts, offset) - 1
        return heading_paths[index] if index >= 0 else None

    paragraphs = [
        (start, start + len(block.text), block.text)
        for block, start in zip(blocks, block_starts, strict=True)
    ]
    chunks = pack_chunks(paragraphs, flush_at=frozenset(heading_starts))

    return [
        Chunk(
            chunk_ordinal=ordinal,
            content=content,
            page_from=None,
            page_to=None,
            locator=locator_at(start),
        )
        for ordinal, (content, start, _end) in enumerate(chunks)
    ]
