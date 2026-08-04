from app.chat.retrieval import Citation

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


def _render_sources(citations: list[Citation]) -> str:
    """Number sources in citation-list order.

    The numbering here has to match the order of the `citations` SSE frame
    exactly — the frontend's citation panel highlights fragment *n* when the
    answer references `[n]`, and that mapping is positional, not by id.
    """
    if not citations:
        return _NO_SOURCES
    lines = []
    for index, citation in enumerate(citations, start=1):
        page_from = citation.get("page_from")
        page_to = citation.get("page_to")
        pages = f"p. {page_from}" if page_from == page_to else f"pp. {page_from}-{page_to}"
        lines.append(f"[{index}] ({pages}) {citation.get('content', '')}")
    return "\n".join(lines)


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
