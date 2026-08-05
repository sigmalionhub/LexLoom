"""The metrics. Pure functions over ranked lists of chunk keys — no database,
no session, no model, nothing to seed.

Kept free of I/O on purpose: every one of these is checked in
`tests/test_evals.py` against an example whose value is worked out by hand in
the docstring of the test. A metric verified by running it and pasting what it
printed is a metric that documents its own bugs.

Three of the five return `float | None`, and the `None` is the load-bearing
part of this module. Six of the thirty questions in the gold set have an empty
relevant set on purpose; recall, reciprocal rank and nDCG are all undefined on
them, and the two tempting ways to make them defined are both wrong in ways
that are hard to see later:

    `or 1.0`   scores a run that returned five irrelevant chunks as perfect,
               on exactly the questions written to catch that.
    `or 0.0`   is a constant. It cannot move with retrieval quality, and it
               drags every average down by a fifth while looking like a
               measurement.

So the undefined cases stay undefined, `mean_of_defined` drops them from the
denominator rather than inventing values for them, and what the unanswerable
questions actually measure lives in `answerability_auc`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from math import log2

# `"gdpr-full:184"` or `"seed:gdpr-excerpts:7"` — `source_key` and
# `chunk_ordinal` joined by a colon, never a UUID. `document_chunks.id` is
# generated at insert (`chat/corpus.py`, `models.py`), so it differs per tenant
# and per reseed; a gold set keyed on one would be stale after its first run.
#
# The seed source key contains a colon of its own, which is why
# `evals/corpus.py` splits these with `rsplit(":", 1)` and never `split(":")`.
ChunkKey = str


def precision_at_k(
    retrieved: Sequence[ChunkKey], relevance: Mapping[ChunkKey, int], k: int
) -> float:
    """Fraction of the top k that is relevant at all (grade >= 1).

    The denominator is `k`, not `len(retrieved)`. A mode that returned three
    chunks for a question with five relevant ones has not earned the same score
    as one that returned five of which three were right — and the lexical mode
    really does return fewer than `k` rows, because a `tsquery` that matches
    nothing matches nothing.

    Defined for every question, including the unanswerable ones, where it is 0
    for any mode that returns anything at all. That constant is why those
    questions are also scored by `answerability_auc`, which can move.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    return sum(1 for key in retrieved[:k] if relevance.get(key, 0) > 0) / k


def recall_at_k(
    retrieved: Sequence[ChunkKey], relevance: Mapping[ChunkKey, int], k: int
) -> float | None:
    """Share of the relevant chunks found in the top k. `None` when there are none.

    `None` — not 0.0 and above all not 1.0. See the module docstring; the
    guard is asserted directly by
    `test_recall_is_undefined_rather_than_perfect_when_nothing_is_relevant`.

    De-duplicates `retrieved` through a set before intersecting: a mode that
    returned the same chunk twice has not found it twice.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    relevant = {key for key, grade in relevance.items() if grade > 0}
    if not relevant:
        return None
    return len(set(retrieved[:k]) & relevant) / len(relevant)


def reciprocal_rank(
    retrieved: Sequence[ChunkKey], relevance: Mapping[ChunkKey, int]
) -> float | None:
    """1/rank of the first relevant chunk. 0.0 if none of them is relevant.

    `None` for the unanswerable questions, for the same reason as
    `recall_at_k`: their reciprocal rank is 0 whatever the system does, because
    there is no hit to rank. Including them would lower the ceiling on MRR by a
    fixed amount while never moving with quality.

    Note the two different zero-ish answers this returns, which are not the same
    statement: `0.0` means "this question has a right answer and the mode did
    not find it in the window", `None` means "this question has no right answer,
    so ranking one is meaningless".
    """
    if not any(grade > 0 for grade in relevance.values()):
        return None
    for rank, key in enumerate(retrieved, start=1):
        if relevance.get(key, 0) > 0:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(
    retrieved: Sequence[ChunkKey], relevance: Mapping[ChunkKey, int], k: int
) -> float | None:
    """Graded gain over the ideal ordering of the same grades. `None` when there is none.

    Linear gain — `grade / log2(rank + 1)` — and not the exponential
    `(2**grade - 1) / log2(rank + 1)` form. With a two-point scale the choice is
    visible and worth stating: linear makes an answering chunk (grade 2) worth
    twice a merely supporting one (grade 1), exponential makes it worth three
    times. Twice is the more conservative reading of a judgement made by one
    author, and it is the form a reader can check with a calculator, which is
    the property this whole module is written for.

    The ideal DCG is computed from the grades in `relevance`, not from the
    grades that happen to appear in `retrieved`: the best possible ranking is
    every judged chunk in grade order, and a mode that returned none of them
    should score 0 rather than 1-out-of-nothing.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    graded = sorted((grade for grade in relevance.values() if grade > 0), reverse=True)
    if not graded:
        return None
    dcg = sum(
        relevance.get(key, 0) / log2(rank + 1)
        for rank, key in enumerate(retrieved[:k], start=1)
    )
    idcg = sum(grade / log2(rank + 1) for rank, grade in enumerate(graded[:k], start=1))
    return dcg / idcg


def answerability_auc(
    answerable_top: Sequence[float], unanswerable_top: Sequence[float]
) -> float | None:
    """Can this mode tell that it found nothing? Threshold-free, and that is the point.

    The probability that a randomly chosen answerable question's best score
    exceeds a randomly chosen unanswerable one's, ties counted as half — the
    Mann-Whitney statistic. 1.0 means every answerable question scored above
    every unanswerable one; 0.5 means the top score carries no information about
    whether the corpus contains an answer at all.

    This is the only metric here that the six unanswerable questions can move.
    `search_chunks` has no score threshold and returns `limit` rows for any
    non-empty `tsquery`, so precision and MRR on those questions are the constant
    0 no matter how good retrieval is.

    Threshold-free because a cut-off would have to be chosen per mode and would
    go stale with the corpus. Scale-free because only the ordering of scores
    enters, which matters: `ts_rank` and cosine similarity share neither range
    nor units, and `retrieval.py` says outright that a `ts_rank` is "comparable
    within one response and nowhere else". Comparing questions against each
    other by rank sidesteps that; comparing raw scores would not.

    Comparable across modes. NOT comparable across corpora, and not meaningful
    at all unless the unanswerable questions are length- and vocabulary-matched
    to the answerable ones — `ts_rank` grows with the number of query lexemes, so
    a set of short unanswerable questions scores well here while measuring
    nothing but question length. `goldset.py` makes both a rule of authorship,
    but only one half of it is enforced: **the test holds length, not
    vocabulary.**
    `test_the_unanswerable_questions_are_length_matched_to_the_answerable_ones`
    counts words and nothing else — it holds the two means within 25 % of each
    other (16.21 against 16.33, a 0.8 % gap) and requires the unanswerable range
    to sit *inside* the answerable one (15-18 words inside 10-24). Vocabulary
    matching is left to the `note` on each question and to review, which is a
    real gap in this metric's guard rail rather than a covered one: a set of
    unanswerable questions written in plain English against answerable ones
    written in GDPR terms would pass that test and still make this number
    measure register rather than answerability.
    """
    if not answerable_top or not unanswerable_top:
        return None
    wins = sum(
        1.0 if a > u else 0.5 if a == u else 0.0
        for a in answerable_top
        for u in unanswerable_top
    )
    return wins / (len(answerable_top) * len(unanswerable_top))


def mean_of_defined(values: Iterable[float | None]) -> float | None:
    """Macro-average over the questions the metric is defined for.

    The whole reason `recall_at_k`, `reciprocal_rank` and `ndcg_at_k` may return
    `None`: this drops those questions from the denominator instead of scoring
    them. `None` when nothing is defined, so an empty gold set reports "not
    measured" rather than a confident zero.

    Macro, not micro — every question weighs the same regardless of how many
    relevant chunks it has. A micro-average weights each question by its
    judgement count instead, and the weights in this gold set are lopsided
    enough for that to matter: 124 judgements over 24 answerable questions,
    `v-principles` alone holding 10 of them. Micro would give that one question
    10/124 (8.1%) of the score against macro's 1/24 (4.2%), and it would tilt
    the whole average towards `verbatim`, the kind carrying the most judgements
    at 36 of 124 — the kind that needs the least of the retriever, since the
    question quotes the text it is looking for. Note the intuition to resist: the
    multi-hop questions are *not* the heavy ones. There are five of them and
    they carry 27 judgements, the fewest of the four answerable kinds.
    """
    defined = [value for value in values if value is not None]
    if not defined:
        return None
    return sum(defined) / len(defined)
