"""Tests for the evaluation harness.

Every metric here is checked against an example whose value is worked out **by
hand in the docstring**, arithmetic and all — not by running the function and
pasting what it printed. A metric verified that way is a metric that documents
its own bugs, and these five numbers are about to become a CI gate and a figure
in a README.

The file is deliberately almost free of the database and entirely free of the
embedding model. What needs a real corpus is the manifest guard, and that needs
only `pdfminer.six`.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import replace
from decimal import ROUND_FLOOR, Decimal

import pytest
from sqlalchemy import text

from app.chat.corpus import SEED_SOURCE_KEY, copy_corpus_into
from app.chat.embedding import get_embedder
from app.db.session import system_session, tenant_session
from app.evals.corpus import (
    CorpusDriftError,
    Manifest,
    SeededChunk,
    assert_corpus_matches_manifest,
    build_corpus,
    build_manifest,
    content_digest,
    corpus_fingerprint,
    load_manifest,
    parse_chunk_key,
)
from app.evals.goldset import (
    ANSWERS,
    EXPECTED_COVERAGE,
    QUESTIONS,
    SUPPORTS,
    goldset_sha256,
    referenced_keys,
)
from app.evals.score import (
    answerability_auc,
    mean_of_defined,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)
from app.ingest.catalogue import CATALOGUE

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


def test_every_floor_records_what_it_was_measured_from_and_why() -> None:
    """A floor is reviewable or it is a number somebody tuned until CI went green.

    The empty skeleton this test used to pin has been filled by Task 27, and the
    thing worth pinning moved with it: not that floors exist, but that each one
    still carries the value it was derived from, the headroom, and a sentence
    saying why that headroom. Lowering a floor then shows up in review as a
    changed `measured` or a changed `why`, which is the question a reviewer has;
    a bare number would let the same edit read as a typo.

    `set_on` and `set_from_commit` are required for the same reason: a threshold
    with no date and no commit cannot be told apart from a threshold that used to
    be true.
    """
    from app.evals.run import FLOORS_FILE

    floors = json.loads(FLOORS_FILE.read_text(encoding="utf-8"))
    assert floors["set_on"], "a floor with no date is not reviewable"
    assert len(floors["set_from_commit"]) == 40, "set_from_commit must be a full SHA"
    assert floors["modes"], "an empty modes object enforces nothing"

    for mode, thresholds in floors["modes"].items():
        for metric, threshold in thresholds.items():
            where = f"{mode}.{metric}"
            assert set(threshold) >= {"floor", "measured", "headroom", "why"}, where
            assert threshold["floor"] < threshold["measured"], (
                f"{where}: a floor at or above the value it was measured from is a "
                "gate that fails on the run that set it"
            )
            assert len(threshold["why"].strip()) > 40, where


def test_every_floor_is_the_number_the_stated_rule_produces() -> None:
    """Evaluate `headroom_rule` and check it against all ten floors.

    This test exists because the file previously described a rule its own numbers
    did not follow: two floors had been recomputed and rounded to nearest while
    the note said "rounded down", and the other eight had their prose re-narrated
    in terms of a new formula without their stored headroom being recomputed —
    eight entries claiming a derivation that had not produced them.

    Prose cannot be checked; arithmetic can. So the rule is stored as data
    (`one_flip`, `mode_factor`, the rounding), and this evaluates it. The point
    is not that these particular constants are correct — that is the reviewer's
    judgement to make — but that whatever the file *says* is what the file *did*.
    A future edit to a floor now has to move the rule or fail here, which is
    exactly the moment the reasoning should be re-read.

    `Decimal`, not float: `0.1479 - 0.020833` is 0.12706699999999998 in binary
    floating point, and a test that has to allow slop for its own arithmetic
    cannot tell a rounding difference from a hand-adjusted number.
    """
    from app.evals.run import FLOORS_FILE

    floors = json.loads(FLOORS_FILE.read_text(encoding="utf-8"))
    rule = floors["headroom_rule"]
    one_flip = Decimal(str(rule["one_flip"]))

    for mode, thresholds in floors["modes"].items():
        factor = Decimal(str(rule["mode_factor"][mode]))
        for metric, threshold in thresholds.items():
            where = f"{mode}.{metric}"
            expected_headroom = (one_flip * factor).quantize(Decimal("0.000001"))
            assert Decimal(str(threshold["headroom"])) == expected_headroom, (
                f"{where}: headroom is {threshold['headroom']}, but the rule in this same "
                f"file yields {expected_headroom}. Recompute it or change the rule; do not "
                "leave the two disagreeing."
            )
            # Rounded DOWN, never to nearest: rounding can only make the gate
            # more permissive than the derivation, so it can never turn a run
            # red on its own.
            expected_floor = (Decimal(str(threshold["measured"])) - expected_headroom).quantize(
                Decimal("0.001"), rounding=ROUND_FLOOR
            )
            assert Decimal(str(threshold["floor"])) == expected_floor, (
                f"{where}: floor is {threshold['floor']}, the rule yields {expected_floor}"
            )
            assert threshold["floor"] < threshold["measured"], (
                f"{where}: a floor at or above the value it was measured from is a "
                "gate that fails on the run that set it"
            )


def test_every_floors_measured_value_still_matches_results_json() -> None:
    """`measured` must not be allowed to rot.

    Checking only `floor < measured` leaves a hole with teeth: a later
    `python -m app.evals.run --write` moves the numbers in `results.json`,
    `floors.json` keeps the old ones, and every headroom in the file silently
    becomes a claim about a measurement that no longer exists. The floors would
    still gate — against the value they were set from — so nothing fails and
    nothing looks wrong. That is the same failure class as a manifest that
    rewrites itself: a record of what was measured that can drift from what was
    measured.

    So re-running `--write` now obliges the author to update `measured` in the
    same commit, which is exactly the moment to ask whether the floor moves too.
    """
    from app.evals.run import FLOORS_FILE, RESULTS_FILE

    floors = json.loads(FLOORS_FILE.read_text(encoding="utf-8"))
    results = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))["modes"]

    for mode, thresholds in floors["modes"].items():
        for metric, threshold in thresholds.items():
            actual = results[mode][metric]
            # `measured` is quoted to four decimals, results.json carries the
            # full float, so the tolerance is half of the last recorded place.
            assert abs(threshold["measured"] - actual) < 0.00005, (
                f"{mode}.{metric}: floors.json records measured="
                f"{threshold['measured']}, results.json now says {actual:.6f}. "
                "Re-derive the floor rather than editing either one to match."
            )


def test_the_floors_file_declares_the_schema_the_runner_reads() -> None:
    """A declared version that nothing validates is decoration."""
    from app.evals.run import FLOORS_SCHEMA, _load_floors

    assert _load_floors()["schema"] == FLOORS_SCHEMA


def test_a_floors_file_from_an_older_schema_is_refused_by_name(tmp_path, monkeypatch) -> None:
    """Version 1 held bare numbers. Read with this code, `threshold["floor"]`
    raises `TypeError: string indices must be integers` from inside the gate,
    which is a confusing way to find out a file is a version behind."""
    from app.evals import run as runner

    older = tmp_path / "floors.json"
    older.write_text(json.dumps({"schema": 1, "modes": {"fts": {"mrr_at_10": 0.65}}}))
    monkeypatch.setattr(runner, "FLOORS_FILE", older)

    with pytest.raises(SystemExit) as error:
        runner._load_floors()

    assert "schema 1" in str(error.value)


def test_no_answerability_floor_is_set_on_a_mode_whose_auc_measures_nothing() -> None:
    """`vector` and `hybrid` may not carry an `answerability_auc` floor, and the
    two are excluded for different reasons — which is why this is a test and not a
    comment.

    `hybrid` scores are RRF values: its top-1 spans exactly [1/61, 2/61] with 19
    distinct values across thirty questions, and an AUC over a near-constant is
    not a measurement. `vector`'s top-1 spans a genuine 0.385-0.855, and it is
    uninformative for the opposite reason — embedding similarity does not
    separate answerable from unanswerable questions on this corpus at all
    (unanswerable top-1 runs 0.490-0.811, straight through the answerable range).
    A floor on either is a flake with no upside; `fts`'s AUC is the one that
    measures something.
    """
    from app.evals.run import FLOORS_FILE

    floors = json.loads(FLOORS_FILE.read_text(encoding="utf-8"))
    for mode in ("vector", "hybrid"):
        assert "answerability_auc" not in floors["modes"].get(mode, {}), mode


def _document_at(**overrides: float) -> dict:
    """A results document holding exactly the committed numbers, with edits."""
    from app.evals.run import RESULTS_FILE

    document = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
    for dotted, value in overrides.items():
        mode, metric = dotted.split("__")
        document["modes"][mode][metric] = value
    return document


def test_a_metric_under_its_floor_names_the_mode_and_the_metric() -> None:
    """The failure has to say what fell, not that something did. A gate whose
    output is "FAILED" sends its reader to re-run the measurement by hand."""
    from app.evals.run import _check_floors

    failures = _check_floors(_document_at(fts__mrr_at_10=0.1))

    assert len(failures) == 1
    assert failures[0].startswith("fts.mrr_at_10: 0.1000 < floor")


def test_the_committed_numbers_clear_every_floor() -> None:
    """The floors were set from these numbers, so this asserts the arithmetic of
    the whole file at once: any floor edited above the value it was derived from
    fails here rather than on the next push."""
    from app.evals.run import _check_floors

    assert _check_floors(_document_at()) == []


def test_a_provisional_floor_says_so_on_every_run(capsys) -> None:
    """The marker exists because the thirty gold-set questions have not been read
    by a human yet, and a marker nobody sees is not a marker. stderr, and on every
    run — not once in a log nobody scrolls back through.

    It does not soften the gate: `_check_floors` returns the same failures either
    way, which the assertion below holds.
    """
    from app.evals.run import _check_floors

    assert _check_floors(_document_at(fts__mrr_at_10=0.1))
    note = capsys.readouterr().err
    assert "PROVISIONAL" in note
    assert "2026-08-05" in note
    assert "floors.json" in note


def test_a_drifting_metric_is_annotated_and_not_failed(capsys) -> None:
    """A runner disagreeing with the machine that wrote results.json by more than
    the measured spread is worth reading and is not worth blocking a merge on:
    what must not regress is already guarded by the floors, against numbers the
    same run produced from the same commit's code. Failing here instead would
    turn a machine difference into a red build, which is how people learn that a
    red gate means "press re-run"."""
    from app.evals.run import _annotate_metric_drift

    _annotate_metric_drift(_document_at(vector__ndcg_at_10=0.9))
    annotations = [line for line in capsys.readouterr().out.splitlines() if line]

    assert len(annotations) == 1
    assert annotations[0].startswith("::warning file=backend/app/evals/results.json::")
    assert "vector.ndcg_at_10" in annotations[0]


def test_an_undrifted_measurement_is_annotated_with_nothing(capsys) -> None:
    from app.evals.run import _annotate_metric_drift

    _annotate_metric_drift(_document_at())

    assert capsys.readouterr().out == ""


def test_report_refuses_to_name_the_file_the_gate_reads() -> None:
    """`--check --report results.json` would be `--write` in a spelling that
    hides it, and a job able to correct its own input cannot fail on it."""
    from app.evals.run import RESULTS_FILE, main

    with pytest.raises(SystemExit) as error:
        main(["--check", "--report", str(RESULTS_FILE)])

    assert error.value.code == 2  # argparse's usage error

# ---------------------------------------------------------------------------
# the gold set
# ---------------------------------------------------------------------------

def test_the_gold_set_has_the_coverage_it_claims() -> None:
    """Thirty questions, and each kind in the number the design fixed.

    Retyping a question from one kind to another silently changes what a
    per-kind regression report means, which is the only thing `kind` is for.
    """
    counts = Counter(question.kind for question in QUESTIONS)
    assert len(QUESTIONS) == 30
    assert dict(counts) == dict(EXPECTED_COVERAGE)

def test_every_question_has_a_unique_id() -> None:
    ids = [question.id for question in QUESTIONS]
    assert len(set(ids)) == len(ids)

def test_every_judgement_records_why_it_was_made() -> None:
    """A judgement with no reason cannot be audited, and this file's whole claim
    is that a sceptic can check it. Grades are 1 or 2 and nothing else: a 0 would
    be an assertion of irrelevance, which the scorers read as absence anyway and
    which nobody would maintain."""
    for question in QUESTIONS:
        for judgement in question.judgements:
            assert judgement.grade in (SUPPORTS, ANSWERS), (question.id, judgement.key)
            assert len(judgement.why.strip()) > 20, (question.id, judgement.key)

def test_every_question_says_what_it_is_for() -> None:
    for question in QUESTIONS:
        assert len(question.note.strip()) > 40, question.id

def test_no_question_names_the_same_chunk_twice() -> None:
    """Two judgements for one chunk would put one grade in `relevance` and lose
    the other, silently."""
    for question in QUESTIONS:
        keys = [judgement.key for judgement in question.judgements]
        assert len(set(keys)) == len(keys), question.id

def test_every_answerable_question_has_a_chunk_that_actually_answers_it() -> None:
    """At least one grade-2 judgement. A question supported from every side and
    answered by nothing is a question whose gold answer is missing."""
    for question in QUESTIONS:
        if question.kind == "unanswerable":
            continue
        assert ANSWERS in question.relevance.values(), question.id

def test_the_unanswerable_questions_carry_no_judgements_at_all() -> None:
    """Empty is a value here, not a missing value — `score.py` branches on it."""
    for question in QUESTIONS:
        if question.kind == "unanswerable":
            assert question.judgements == (), question.id
            assert not question.is_answerable

def test_the_unanswerable_questions_are_length_matched_to_the_answerable_ones() -> None:
    """The rule `answerability_auc` depends on, enforced instead of reviewed.

    `ts_rank` grows with the number of query lexemes, so a set of short, simple
    unanswerable questions against long, specialist answerable ones produces a
    splendid AUC that measures nothing but question length. The two means are
    held within 25 % of each other and the ranges are required to overlap, which
    is as much as a test can check about vocabulary matching; the rest is in the
    `note` on each question.
    """
    answerable = [len(q.text.split()) for q in QUESTIONS if q.is_answerable]
    unanswerable = [len(q.text.split()) for q in QUESTIONS if not q.is_answerable]
    mean_a = sum(answerable) / len(answerable)
    mean_u = sum(unanswerable) / len(unanswerable)
    assert abs(mean_u - mean_a) / mean_a < 0.25, (mean_a, mean_u)
    assert min(unanswerable) >= min(answerable), (min(answerable), min(unanswerable))
    assert max(unanswerable) <= max(answerable), (max(answerable), max(unanswerable))

def test_the_gold_set_hash_ignores_prose_and_notices_a_grade() -> None:
    """`goldset_sha256` is over the questions and relevances, not the file bytes.

    A clarified `why` must not force a re-measurement of something nothing
    changed; a changed grade must.
    """
    baseline = goldset_sha256(QUESTIONS)
    first = QUESTIONS[0]
    reworded = replace(
        first,
        note="reworded entirely",
        judgements=tuple(replace(j, why="reworded entirely") for j in first.judgements),
    )
    assert goldset_sha256((reworded, *QUESTIONS[1:])) == baseline

    regraded = replace(
        first,
        judgements=(replace(first.judgements[0], grade=SUPPORTS), *first.judgements[1:]),
    )
    assert goldset_sha256((regraded, *QUESTIONS[1:])) != baseline

# ---------------------------------------------------------------------------
# the gold set against the real corpus (pdfminer.six only: no database, no model)
# ---------------------------------------------------------------------------

def test_the_parsed_corpus_is_the_one_the_manifest_was_written_for() -> None:
    """The drift guard, run against the committed manifest and the real PDF.

    This is the test that goes red when the chunker changes — before any metric
    does, and with an error that names the ordinal and both hashes instead of a
    number that looks like a search regression.
    """
    assert_corpus_matches_manifest(build_corpus(), load_manifest())

def test_the_corpus_is_1008_chunks_from_two_sources() -> None:
    """978 catalogue chunks and 30 seeded excerpts, measured rather than
    estimated. The catalogue's own `expected_chunks` is the other place this
    number is written down, and they have to agree.

    Was 1057 (1027 + 30) while the parser used `pypdf`. The catalogue chunk
    count moved to 978 when `pdfminer.six` replaced it, because pypdf's
    word-splitting had inflated the extracted text by 13,863 characters of
    spurious spaces; see `catalogue.py` and `task-22-report.md`. The gold set
    was re-bound onto the new ordinals in task-26-rebind-report.md — this number
    changing is what forced that work, and it is asserted here so a future
    extractor change cannot move it quietly.
    """
    chunks = build_corpus()
    assert len(chunks) == 1008
    assert Counter(chunk.source_key for chunk in chunks) == {
        "gdpr-full": 978,
        "seed:gdpr-excerpts": 30,
    }
    assert CATALOGUE["gdpr-full"].expected_chunks == 978

def test_every_chunk_the_gold_set_names_exists_in_the_corpus() -> None:
    """A judgement about a chunk that is not there scores as a permanent miss and
    is indistinguishable from a retrieval failure."""
    present = {chunk.key for chunk in build_corpus()}
    missing = [key for key in referenced_keys(QUESTIONS) if key not in present]
    assert missing == []

def test_every_chunk_in_the_corpus_is_addressable() -> None:
    """A chunk with no `(source_key, chunk_ordinal)` is one the gold set cannot
    name, and it can still be returned by a search.

    Checked here on the data, where it is cheap, and again in the runner against
    the database after seeding — because the column being populated in
    `build_corpus` and the column being populated in `document_chunks` are two
    different claims, and it was the second one that was broken before Task 22.
    """
    for chunk in build_corpus():
        assert chunk.source_key
        assert chunk.chunk_ordinal is not None
        assert parse_chunk_key(chunk.key) == (chunk.source_key, chunk.chunk_ordinal)

# ---------------------------------------------------------------------------
# the two preconditions that need something outside this package
# ---------------------------------------------------------------------------

async def test_every_seeded_chunk_in_a_tenants_corpus_is_addressable(
    created_orgs: list[str],
) -> None:
    """A NULL `source_key` is a chunk the gold set cannot name, and it can still
    be returned.

    The seeded excerpts are extracts of the same GDPR text the catalogue source
    holds, so a correct answer can arrive as the seeded duplicate. Scored against
    a gold set keyed on `(source_key, chunk_ordinal)`, an unaddressable duplicate
    counts as a miss — the metric drops and retrieval had nothing to do with it.
    Task 22 backfilled these columns for `copy_corpus_into`; this test is what
    makes the omission loud instead of turning it into a plausible number.

    Run against a throwaway organisation rather than against the eval one on
    purpose: `copy_corpus_into` is the code path that had the defect, it needs no
    embedding model (the vectors come from the committed file), and it finishes
    in milliseconds. The same property is checked against the eval organisation's
    full 1008-chunk corpus by `run.py`, after seeding and before the first search.
    """
    org_id = str(uuid.uuid4())
    async with system_session() as session:
        await session.execute(
            text("INSERT INTO organizations (id, external_id, name) VALUES (:i, :e, :n)"),
            {"i": org_id, "e": f"ext_{org_id}", "n": "Eval precondition"},
        )
    created_orgs.append(org_id)

    async with tenant_session(org_id) as session:
        written = await copy_corpus_into(session, org_id)

    async with tenant_session(org_id) as session:
        unnamed = await session.scalar(
            text(
                "SELECT count(*) FROM document_chunks "
                "WHERE org_id = :org AND (source_key IS NULL OR chunk_ordinal IS NULL)"
            ),
            {"org": org_id},
        )
        ordinals = (
            await session.execute(
                text(
                    "SELECT chunk_ordinal FROM document_chunks "
                    "WHERE org_id = :org AND source_key = :key ORDER BY chunk_ordinal"
                ),
                {"org": org_id, "key": SEED_SOURCE_KEY},
            )
        ).scalars().all()

    assert written == 30
    assert unnamed == 0, f"{unnamed} chunks carry no stable key and cannot be scored"
    assert list(ordinals) == list(range(30))

@pytest.mark.model
def test_the_same_question_embeds_to_the_same_vector_twice_in_one_process() -> None:
    """The property `run.py` relies on instead of memoising the embedder.

    The plan asked for one inference per question so that `vector` and `hybrid`
    are compared on the same vector rather than on two numbers with one name.
    Reaching into the embedder to memoise it would be the one shortcut in a
    runner whose entire claim is that it takes the real path, so the property is
    checked here directly instead: two calls, same input, identical output. If
    this ever goes red, `measure()` is comparing two modes on two vectors and the
    memo has to go in after all.

    Marked `model` because it needs the real ONNX weights — and it needs them by
    the indirect route, through `get_embedder()`, which is exactly the shape of
    dependency that has twice been missed in this milestone.
    """
    embedder = get_embedder()
    question = QUESTIONS[0].text
    assert embedder.encode_query(question) == embedder.encode_query(question)
