"""Tests for the evaluation harness.

Every metric here is checked against an example whose value is worked out **by
hand in the docstring**, arithmetic and all — not by running the function and
pasting what it printed. A metric verified that way is a metric that documents
its own bugs, and these five numbers are about to become a CI gate and a figure
in a README.

The file is deliberately almost free of the database and entirely free of the
embedding model. What needs a real corpus is the manifest guard, and that needs
only `pypdf`.
"""

from __future__ import annotations

import json

import pytest

from app.evals.corpus import (
    CorpusDriftError,
    Manifest,
    SeededChunk,
    assert_corpus_matches_manifest,
    build_manifest,
    content_digest,
    corpus_fingerprint,
    parse_chunk_key,
)
from app.evals.score import (
    answerability_auc,
    mean_of_defined,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

# ---------------------------------------------------------------------------
# precision@k
# ---------------------------------------------------------------------------


def test_precision_at_k_counts_any_grade_above_zero() -> None:
    """Four retrieved, two of them judged, k=3.

    Ranks 1..3 are gdpr-full:10 (ungraded, 0), gdpr-full:11 (grade 2, counts)
    and gdpr-full:12 (ungraded, 0). gdpr-full:13 is graded but sits at rank 4
    and is outside the window.

        hits in the top 3 = 1
        precision@3 = 1 / 3 = 0.333333...
    """
    retrieved = ["gdpr-full:10", "gdpr-full:11", "gdpr-full:12", "gdpr-full:13"]
    relevance = {"gdpr-full:11": 2, "gdpr-full:13": 1}
    assert precision_at_k(retrieved, relevance, 3) == pytest.approx(1 / 3)


def test_precision_divides_by_k_and_not_by_how_many_rows_came_back() -> None:
    """One row returned, and it is right. That is not the same as three-for-three.

    The lexical mode really does return fewer than k rows — a tsquery that
    matches nothing matches nothing — so this is the difference between
    "precise" and "quiet".

        hits in the top 3 = 1, denominator = k = 3
        precision@3 = 1 / 3 = 0.333333...

    Dividing by len(retrieved) would give 1 / 1 = 1.0 and score a mode that
    found one chunk out of five relevant ones as perfect.
    """
    assert precision_at_k(["gdpr-full:11"], {"gdpr-full:11": 2}, 3) == pytest.approx(1 / 3)


def test_precision_is_defined_and_zero_for_an_unanswerable_question() -> None:
    """Nothing is relevant, three rows came back, none of them can be a hit.

        hits = 0, precision@3 = 0 / 3 = 0.0

    Defined, unlike recall — and constant, which is why `answerability_auc`
    exists.
    """
    assert precision_at_k(["gdpr-full:1", "gdpr-full:2", "gdpr-full:3"], {}, 3) == 0.0


# ---------------------------------------------------------------------------
# recall@k
# ---------------------------------------------------------------------------


def test_recall_at_k_is_over_the_relevant_set_not_the_window() -> None:
    """Four chunks are relevant; the top five contain two of them.

        retrieved[:5] = a, x, c, y, z
        relevant      = {a, b, c, d}
        intersection  = {a, c} -> 2
        recall@5 = 2 / 4 = 0.5
    """
    retrieved = ["a", "x", "c", "y", "z"]
    relevance = {"a": 2, "b": 1, "c": 2, "d": 1}
    assert recall_at_k(retrieved, relevance, 5) == pytest.approx(0.5)


def test_recall_does_not_count_a_duplicate_row_twice() -> None:
    """A mode that returned the same chunk twice has not found it twice.

        retrieved[:5] = a, a, x, y, z
        relevant      = {a, b}
        set(retrieved[:5]) & relevant = {a} -> 1
        recall@5 = 1 / 2 = 0.5

    Without the set, the naive count would be 2 / 2 = 1.0.
    """
    assert recall_at_k(["a", "a", "x", "y", "z"], {"a": 2, "b": 1}, 5) == pytest.approx(0.5)


def test_recall_is_undefined_rather_than_perfect_when_nothing_is_relevant() -> None:
    """The unanswerable questions must not be scorable as successes.

    Sabotage for this one is the two-character fix a hurried reader makes:
    change the guard to `return 1.0`. This test must go red — and it does; the
    run is in the task report.

    `0.0` is the other tempting answer and is just as wrong: it is a constant
    that cannot move with retrieval quality while dragging the average down by a
    fifth, so a real improvement and a real regression both leave it alone.
    """
    assert recall_at_k(["gdpr-full:1", "gdpr-full:2"], {}, 5) is None


# ---------------------------------------------------------------------------
# reciprocal rank
# ---------------------------------------------------------------------------


def test_reciprocal_rank_is_one_over_the_first_hit() -> None:
    """The first relevant chunk is at rank 3.

        ranks:  1 -> x (0), 2 -> y (0), 3 -> a (grade 2, hit)
        RR = 1 / 3 = 0.333333...
    """
    assert reciprocal_rank(["x", "y", "a"], {"a": 2}) == pytest.approx(1 / 3)


def test_reciprocal_rank_is_zero_when_a_hit_exists_and_was_missed() -> None:
    """There is a right answer; the window does not contain it.

        RR = 0.0

    Not `None`: `None` means "ranking an answer is meaningless here", and it
    would drop this question from the MRR denominator — quietly excusing exactly
    the failure the metric exists to see.
    """
    assert reciprocal_rank(["x", "y", "z"], {"a": 2}) == 0.0


def test_reciprocal_rank_is_undefined_when_there_is_no_answer_to_rank() -> None:
    assert reciprocal_rank(["x", "y", "z"], {}) is None


# ---------------------------------------------------------------------------
# nDCG@k
# ---------------------------------------------------------------------------


def test_ndcg_at_k_discounts_by_position_against_the_ideal_ordering() -> None:
    """Two chunks of grade 2, retrieved at ranks 2 and 3 instead of 1 and 2.

    Linear gain, `grade / log2(rank + 1)`:

        DCG  = 0/log2(2) + 2/log2(3) + 2/log2(4)
             = 0 + 2/1.584962500721156 + 2/2
             = 1.261859507142915 + 1.0
             = 2.261859507142915

        ideal ordering of the same grades is [2, 2] at ranks 1 and 2:
        IDCG = 2/log2(2) + 2/log2(3)
             = 2.0 + 1.261859507142915
             = 3.261859507142915

    The numerator is the denominator minus exactly 1.0, so

        nDCG = 1 - 1/3.261859507142915 = 1 - 0.30657363 = 0.69342637
    """
    retrieved = ["x", "a", "b"]
    relevance = {"a": 2, "b": 2}
    assert ndcg_at_k(retrieved, relevance, 3) == pytest.approx(0.693426, abs=1e-6)


def test_ndcg_is_exactly_one_for_the_ideal_ordering() -> None:
    """Grades 2 then 1, retrieved in that order.

        DCG  = 2/log2(2) + 1/log2(3) = 2.0 + 0.630929753571457 = 2.630929753571457
        IDCG = the same sum over the same grades in the same order
        nDCG = 1.0, exactly
    """
    assert ndcg_at_k(["a", "b"], {"a": 2, "b": 1}, 10) == 1.0


def test_ndcg_ranks_the_answering_chunk_above_the_supporting_one() -> None:
    """The grades are what makes this different from precision.

    Same two chunks, both retrieved, only the order differs.

        best order (2 then 1): DCG = 2/1 + 1/1.584962500721156 = 2.630929753571457
        worse order (1 then 2): DCG = 1/1 + 2/1.584962500721156 = 2.261859507142915

    IDCG is 2.630929753571457 either way, so the worse order scores

        2.261859507142915 / 2.630929753571457
            = 1 - 0.369070246428543 / 2.630929753571457
            = 1 - 0.1402813
            = 0.8597187

    Precision@2 is 1.0 for both, which is the whole argument for a graded scale.
    """
    ideal = ndcg_at_k(["a", "b"], {"a": 2, "b": 1}, 2)
    swapped = ndcg_at_k(["b", "a"], {"a": 2, "b": 1}, 2)
    assert ideal == 1.0
    assert swapped == pytest.approx(0.8597187, abs=1e-6)
    assert precision_at_k(["a", "b"], {"a": 2, "b": 1}, 2) == precision_at_k(
        ["b", "a"], {"a": 2, "b": 1}, 2
    )


def test_ndcg_is_zero_when_nothing_relevant_was_retrieved_not_one() -> None:
    """The ideal is computed from the judgements, not from what came back.

        DCG  = 0 (no retrieved chunk carries a grade)
        IDCG = 2/log2(2) = 2.0 (there is a grade-2 chunk in the gold set)
        nDCG = 0 / 2 = 0.0

    Taking the ideal from the retrieved grades instead would give 0/0 and the
    tempting rescue for that is 1.0 — a perfect score for finding nothing.
    """
    assert ndcg_at_k(["x", "y"], {"a": 2}, 10) == 0.0


def test_ndcg_is_undefined_when_there_is_nothing_to_rank() -> None:
    assert ndcg_at_k(["x", "y"], {}, 10) is None


# ---------------------------------------------------------------------------
# answerability AUC
# ---------------------------------------------------------------------------


def test_auc_is_one_when_every_answerable_question_outscores_every_other() -> None:
    """2 x 2 = 4 pairs, all won: 4 / 4 = 1.0. And a single tied pair is 0.5."""
    assert answerability_auc([0.9, 0.8], [0.2, 0.1]) == 1.0
    assert answerability_auc([0.5], [0.5]) == 0.5


def test_auc_counts_ties_as_half_a_win() -> None:
    """Two answerable, one unanswerable, one pair won and one lost.

        pairs: (0.9 vs 0.5) -> 1.0
               (0.1 vs 0.5) -> 0.0
        AUC = 1.0 / 2 = 0.5

    0.5 is the "this mode's top score says nothing about whether the corpus can
    answer at all" reading, and it is reached here by half the pairs going each
    way rather than by ties. Both routes to 0.5 mean the same thing.
    """
    assert answerability_auc([0.9, 0.1], [0.5]) == pytest.approx(0.5)


def test_auc_is_zero_when_the_unanswerable_questions_score_higher() -> None:
    """A real possibility rather than a hypothetical: `ts_rank` grows with the
    number of matched lexemes, so a long unanswerable question about a
    neighbouring topic can outscore a short answerable one.

        pairs: (0.1 vs 0.9) -> 0.0
        AUC = 0 / 1 = 0.0
    """
    assert answerability_auc([0.1], [0.9]) == 0.0


def test_auc_is_undefined_when_one_side_is_empty() -> None:
    """A gold set with no unanswerable questions cannot measure this, and saying
    so is not the same as reporting 0.5."""
    assert answerability_auc([0.9], []) is None
    assert answerability_auc([], [0.1]) is None


# ---------------------------------------------------------------------------
# the aggregator
# ---------------------------------------------------------------------------


def test_the_recall_average_drops_the_undefined_questions_rather_than_counting_them() -> None:
    """24 answerable questions at 0.5 average 0.5, not 0.4 — the six Nones are not zeros.

        counting them as zeros: (24 * 0.5 + 6 * 0.0) / 30 = 12.0 / 30 = 0.4
        dropping them:          (24 * 0.5) / 24          = 12.0 / 24 = 0.5

    The difference is a fifth of the score, it is constant, and it would be
    indistinguishable from a real regression to anyone reading the number rather
    than this function.
    """
    values = [0.5] * 24 + [None] * 6
    assert mean_of_defined(values) == pytest.approx(0.5)


def test_the_average_of_nothing_defined_is_undefined() -> None:
    assert mean_of_defined([None, None]) is None
    assert mean_of_defined([]) is None


# ---------------------------------------------------------------------------
# chunk keys
# ---------------------------------------------------------------------------


def test_a_seed_chunk_key_survives_the_colon_in_its_own_source_key() -> None:
    """`SEED_SOURCE_KEY` is "seed:gdpr-excerpts", so the key has two colons.

    `split(":")` from the left names the source "seed" and the ordinal
    "gdpr-excerpts". On a good day that is a ValueError; on a bad one somebody
    takes element [0] and every seeded judgement silently addresses a source
    that does not exist.
    """
    assert parse_chunk_key("seed:gdpr-excerpts:7") == ("seed:gdpr-excerpts", 7)
    assert parse_chunk_key("gdpr-full:184") == ("gdpr-full", 184)


def test_a_key_without_a_source_is_refused() -> None:
    with pytest.raises(ValueError, match="not a chunk key"):
        parse_chunk_key("184")


# ---------------------------------------------------------------------------
# the corpus fingerprint
# ---------------------------------------------------------------------------


def _chunk(source_key: str, ordinal: int, content: str) -> SeededChunk:
    return SeededChunk(
        source_key=source_key, chunk_ordinal=ordinal, content=content, page_from=1, page_to=1
    )


def test_the_fingerprint_does_not_depend_on_the_order_the_chunks_arrive_in() -> None:
    """It hashes a sorted view, so re-running on the same inputs on a different
    machine gives the same value — which is the only thing that makes it usable
    as a provenance field rather than a nuisance to regenerate."""
    a = _chunk("gdpr-full", 0, "one")
    b = _chunk("gdpr-full", 1, "two")
    assert corpus_fingerprint([a, b]) == corpus_fingerprint([b, a])


def test_the_fingerprint_moves_when_an_ordinal_points_at_different_text() -> None:
    """The exact drift the gold set cannot survive, and the only thing this hash
    is for."""
    before = corpus_fingerprint([_chunk("gdpr-full", 0, "one"), _chunk("gdpr-full", 1, "two")])
    after = corpus_fingerprint([_chunk("gdpr-full", 0, "one"), _chunk("gdpr-full", 1, "TWO")])
    assert before != after


def test_the_fingerprint_moves_when_the_corpus_is_renumbered() -> None:
    """Same texts, shifted ordinals — a chunker change that adds one chunk near
    the front does exactly this to everything after it."""
    before = corpus_fingerprint([_chunk("gdpr-full", 0, "one"), _chunk("gdpr-full", 1, "two")])
    after = corpus_fingerprint([_chunk("gdpr-full", 1, "one"), _chunk("gdpr-full", 2, "two")])
    assert before != after


# ---------------------------------------------------------------------------
# the manifest guard
# ---------------------------------------------------------------------------


def _small_corpus() -> list[SeededChunk]:
    return [
        _chunk("seed:gdpr-excerpts", 0, "a seeded excerpt"),
        _chunk("gdpr-full", 0, "the first parsed chunk"),
        _chunk("gdpr-full", 1, "the second parsed chunk"),
    ]


def test_the_manifest_pins_only_the_chunks_the_gold_set_names() -> None:
    """Sixty-odd hashes of a thousand chunks: reviewable in a diff, and complete
    over everything a metric can depend on. The per-source counts cover the
    rest."""
    manifest = build_manifest(_small_corpus(), ["gdpr-full:1"])
    assert manifest.sources == {"gdpr-full": 2, "seed:gdpr-excerpts": 1}
    assert manifest.total == 3
    assert manifest.chunks == {
        "gdpr-full:1": content_digest("the second parsed chunk")[:16]
    }


def test_a_manifest_cannot_be_written_for_a_chunk_that_does_not_exist() -> None:
    with pytest.raises(CorpusDriftError, match="which the corpus does not contain"):
        build_manifest(_small_corpus(), ["gdpr-full:99"])


def test_the_guard_passes_on_the_corpus_it_was_written_for() -> None:
    corpus = _small_corpus()
    assert_corpus_matches_manifest(corpus, build_manifest(corpus, ["gdpr-full:0", "gdpr-full:1"]))


def test_the_guard_names_the_source_whose_count_changed() -> None:
    """A chunker change first shows up as a different number of chunks, and
    naming the source localises it better than a hash mismatch on one ordinal
    ever could."""
    corpus = _small_corpus()
    manifest = build_manifest(corpus, ["gdpr-full:0"])
    grown = [*corpus, _chunk("gdpr-full", 2, "a third parsed chunk")]
    with pytest.raises(CorpusDriftError, match="gdpr-full"):
        assert_corpus_matches_manifest(grown, manifest)


def test_the_guard_catches_a_renumbering_that_keeps_the_count(monkeypatch) -> None:
    """The dangerous case: the same number of chunks, different text under the
    same ordinal. Nothing about the counts is wrong; every judgement is."""
    corpus = _small_corpus()
    manifest = build_manifest(corpus, ["gdpr-full:0", "gdpr-full:1"])
    shifted = [
        _chunk("seed:gdpr-excerpts", 0, "a seeded excerpt"),
        _chunk("gdpr-full", 0, "the second parsed chunk"),
        _chunk("gdpr-full", 1, "the first parsed chunk"),
    ]
    with pytest.raises(CorpusDriftError) as error:
        assert_corpus_matches_manifest(shifted, manifest)
    message = str(error.value)
    assert "gdpr-full:0" in message
    assert "Re-derive the gold set" in message


def test_the_guard_reports_both_hashes_so_the_reader_can_tell_which_side_moved() -> None:
    corpus = _small_corpus()
    manifest = Manifest(
        sources={"gdpr-full": 2, "seed:gdpr-excerpts": 1},
        chunks={"gdpr-full:0": "0123456789abcdef"},
    )
    with pytest.raises(CorpusDriftError) as error:
        assert_corpus_matches_manifest(corpus, manifest)
    message = str(error.value)
    assert "0123456789abcdef" in message
    assert content_digest("the first parsed chunk")[:16] in message


# ---------------------------------------------------------------------------
# floors.json
# ---------------------------------------------------------------------------


def test_the_committed_floors_are_an_empty_skeleton_and_say_why() -> None:
    """Task 27 puts numbers here once it has measured the run-to-run spread.

    Pinned so that a floor arriving without that measurement is a change to a
    test rather than a quiet edit to a JSON file — the exact shape of "tune the
    threshold until the gate is green".
    """
    from app.evals.run import FLOORS_FILE

    floors = json.loads(FLOORS_FILE.read_text(encoding="utf-8"))
    assert floors["modes"] == {}
    assert "spread" in floors["note"]
