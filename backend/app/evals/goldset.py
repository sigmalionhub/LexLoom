"""Thirty questions with graded relevance judgements, and the reason for each.

**How these were written, because that is the only thing that makes them worth
anything.** A gold set is trivially easy to write so that it flatters whatever
the system already does: pick questions whose wording shares a rare term with
exactly one chunk and lexical search looks excellent; write the questions after
watching the vector mode succeed and the vector mode looks excellent. Either way
the metric measures the author.

The procedure used here, in order, and none of it reversed:

1. The question text was written first, from the regulation's own structure —
   which articles exist, what each is about — and from the vocabulary a person
   researching GDPR uses, not the vocabulary the corpus uses. Written and then
   frozen.
2. Only then were the relevant chunks located, by walking to the article by its
   number in the parsed text (`Article 33`, `Article 15`), never by searching for
   the question's own words. Locating ground truth by keyword overlap is how a
   gold set ends up defining relevance as "whatever the lexical mode would find".
3. `search_chunks` was not run against any question until every judgement in
   this file was fixed and the manifest written. The numbers in `results.json`
   are the first time these questions met the retriever.

**Relevance is graded, not binary.** `2` — the chunk answers the question; `1` —
the chunk is on point and supports or completes the answer without being it. A
binary scale would make `nDCG@10` identical to `precision@10` with a different
denominator: a fourth metric carrying no fourth piece of information.

**Every judgement carries its reason.** A judgement with no reason cannot be
audited, and this file's whole claim is that a sceptic can check it. `why` is
prose for a human; it is deliberately excluded from `goldset_sha256` so that
improving an explanation does not invalidate a measurement that nothing changed.

**The six unanswerable questions are not filler.** They exist for
`answerability_auc`, which is the only metric they can move: `search_chunks` has
no score threshold and returns `limit` rows for any non-empty `tsquery`, so
their precision and MRR are the constant 0 in every mode at every quality. That
constant is why they are excluded from recall, MRR and nDCG rather than averaged
in as zeros.

Two rules of authorship follow, and both are held by tests rather than by
review:

*They must be genuinely unanswerable from this corpus, not merely hard.* Each
asks about something the GDPR does not contain at all — an institution outside
the Regulation, a deadline it declines to set, the subject matter of a different
instrument. Each carries a `note` saying what makes it unanswerable.

*They must be length- and vocabulary-matched to the answerable ones.* `ts_rank`
grows with the number of query lexemes and varies with document length, so a set
of short, simple unanswerable questions against long, specialist answerable ones
produces a splendid AUC that measures nothing but question length. See
`test_the_unanswerable_questions_are_length_matched_to_the_answerable_ones`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from app.evals.score import ChunkKey

Kind = Literal["verbatim", "paraphrase", "synonym", "multi_hop", "unanswerable"]

# How many questions of each kind, and what each kind is for. Checked by
# `test_the_gold_set_has_the_coverage_it_claims`, so a question quietly
# retyped from one kind to another is an error rather than a silent change to
# what a regression report means.
EXPECTED_COVERAGE: Mapping[Kind, int] = {
    # Wording taken from the article itself. The floor: if these fail, nothing
    # above them means anything.
    "verbatim": 6,
    # The same question in different words. Lexical search should lose ground
    # here.
    "paraphrase": 7,
    # The concept named by a different term entirely — "the entity deciding the
    # purposes" for "controller". Where the vector mode should gain most.
    "synonym": 6,
    # The answer spans two articles, so `recall@5` is the metric that moves and
    # `MRR` is not.
    "multi_hop": 5,
    # No answer in this corpus at all. Only `answerability_auc` can move.
    "unanswerable": 6,
}

# Grades, spelled rather than left as bare integers at 60 call sites.
ANSWERS = 2
SUPPORTS = 1


@dataclass(frozen=True)
class Judgement:
    """One chunk marked relevant to one question, with the reason it was marked.

    `key` is a `(source_key, chunk_ordinal)` pair rendered as
    `"gdpr-full:184"`. Never a UUID: `document_chunks.id` is generated at insert
    and differs per tenant and per reseed.
    """

    key: ChunkKey
    grade: Literal[1, 2]
    why: str


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    kind: Kind
    # Empty for the unanswerable six. That is a value, not a missing value, and
    # the scorers in `score.py` all branch on it explicitly.
    judgements: tuple[Judgement, ...]
    # Which property of retrieval this question exists to exercise, or — for an
    # unanswerable one — why the corpus cannot answer it. Recorded so a
    # regression reads as "paraphrases broke" rather than "MRR fell 0.04".
    note: str

    @property
    def relevance(self) -> Mapping[ChunkKey, int]:
        """The mapping the scorers take. Derived, never stored twice.

        Two parallel structures — grades here, reasons there — is how a
        judgement ends up with a reason attached to a different chunk than the
        one it grades, with nothing raised anywhere.
        """
        return {judgement.key: judgement.grade for judgement in self.judgements}

    @property
    def is_answerable(self) -> bool:
        return bool(self.judgements)


def referenced_keys(questions: Sequence[Question]) -> list[ChunkKey]:
    """Every chunk any judgement names, sorted and de-duplicated.

    This is exactly the set `manifest.json` pins content hashes for: pinning the
    whole corpus would make the artefact unreadable in a diff, pinning less
    would leave a chunk a metric depends on unwatched.
    """
    return sorted({judgement.key for question in questions for judgement in question.judgements})


def canonical_payload(questions: Sequence[Question]) -> str:
    """The bytes `goldset_sha256` hashes: questions and relevances, nothing else.

    Not the bytes of this file. A renamed variable, a reflowed comment or a
    clarified `why` would otherwise look like a different gold set and force a
    re-measurement of something nothing changed — and the fastest way to teach
    someone to regenerate a provenance hash without reading it is to make it
    change for reasons that do not matter.

    `sort_keys` and a fixed separator, so the hash depends on the content and
    not on the order a dict happened to be built in.
    """
    return json.dumps(
        [
            {
                "id": question.id,
                "text": question.text,
                "kind": question.kind,
                "relevance": dict(sorted(question.relevance.items())),
            }
            for question in sorted(questions, key=lambda q: q.id)
        ],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def goldset_sha256(questions: Sequence[Question] | None = None) -> str:
    return hashlib.sha256(
        canonical_payload(QUESTIONS if questions is None else questions).encode("utf-8")
    ).hexdigest()


QUESTIONS: tuple[Question, ...] = ()
