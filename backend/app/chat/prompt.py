from app.chat.retrieval import Citation
from app.ingest.catalogue import CATALOGUE

# Three clauses, and the third is the one that matters. Told only to answer from
# the sources, a model that finds nothing will still answer — inventing a
# plausible article number, formatted exactly like a real citation. That is the
# single worst failure mode a legal-research demo can have, because it is
# indistinguishable from a correct answer to anyone who does not already know
# the corpus. Saying "if the sources do not contain the answer, say so" is what
# turns a confident fabrication into a visible gap.
_INSTRUCTIONS = (
    "You are a legal research assistant. Answer strictly from the numbered "
    "sources below and nothing else — do not draw on outside knowledge.\n"
    "When a statement in your answer is supported by a source, cite it inline "
    "with its number in brackets, like [1], matching the numbering below "
    "exactly.\n"
    "If the sources do not contain the answer, say so plainly instead of "
    "guessing or inventing a citation."
)

_NO_SOURCES = "(No sources were found for this question.)"


def _locate(citation: Citation) -> str:
    """Where this chunk is, in whatever way its document can say it.

    A paginated source says it with page numbers. A source whose format has no
    pages — the `.docx` built from a web publication — says it with the heading
    path migration 0009's `locator` column carries, and its page columns are
    NULL.

    The `page_from is None` test comes first and is not optional: `None ==
    None` is true, so the old equality branch rendered `p. None` for every
    page-less chunk and fed it to the model as if it were a position.
    """
    page_from = citation.get("page_from")
    page_to = citation.get("page_to")
    if page_from is None or page_to is None:
        locator = citation.get("locator")
        return str(locator) if locator else "no locator"
    if page_from == page_to:
        return f"p. {page_from}"
    return f"pp. {page_from}-{page_to}"


def _render_sources(citations: list[Citation]) -> str:
    """Number sources in citation-list order, under a legend of the documents.

    The numbering here has to match the order of the `citations` SSE frame
    exactly — the frontend's citation panel highlights fragment *n* when the
    answer references `[n]`, and that mapping is positional, not by id.

    Documents are named once, in a legend, and each citation carries only its
    short key. Naming the document inline was the obvious alternative and is
    measurably wasteful: the GDPR's catalogue title is 280 characters and
    `search_chunks` runs at a default limit of 5, so the inline form would put
    roughly 1,400 characters of identical text in one prompt. The legend costs
    one line per distinct document actually cited.
    """
    if not citations:
        return _NO_SOURCES

    keys = list(dict.fromkeys(str(c.get("source_key") or "") for c in citations))
    legend = [f"{key} = {CATALOGUE[key].title}" for key in keys if key and key in CATALOGUE]

    lines = []
    for index, citation in enumerate(citations, start=1):
        key = str(citation.get("source_key") or "")
        where = f"{key}, {_locate(citation)}" if key else _locate(citation)
        lines.append(f"[{index}] ({where}) {citation.get('content', '')}")

    if not legend:
        return "\n".join(lines)
    return "Documents:\n" + "\n".join(legend) + "\n\n" + "\n".join(lines)


def build_grounded_prompt(question: str, citations: list[Citation]) -> str:
    """Assemble the only thing that keeps this a research tool rather than a chatbot.

    Three clauses, and the third is the one that matters. Told only to answer
    from the sources, a model that finds nothing will still answer — inventing
    a plausible article number, formatted exactly like a real citation. That is
    the single worst failure mode a legal-research demo can have, because it is
    indistinguishable from a correct answer to anyone who does not already know
    the corpus. Saying "if the sources do not contain the answer, say so" is
    what turns a confident fabrication into a visible gap.

    A pure function on purpose: no network, no database session, so the prompt
    a provider actually receives can be asserted against in a unit test with no
    infrastructure at all — see test_prompt.py.
    """
    return f"{_INSTRUCTIONS}\n\nSources:\n{_render_sources(citations)}\n\nQuestion: {question}\n"
