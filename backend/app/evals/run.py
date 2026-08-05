"""The runner: every question through the real search path, and a provenance
record that cannot be forged by accident.

    python -m app.evals.run --write            # reseed, measure, rewrite results.json
    python -m app.evals.run --write-manifest   # reseed nothing, rewrite manifest.json
    python -m app.evals.run --check            # reseed, measure, compare to floors

    python -m app.evals.run --check --report evals-ci.json   # ...and publish what it measured

`--report` writes the recomputed document somewhere that is not
`results.json`, which is the only way the numbers behind a *failed* gate ever
leave the runner: the CI job uploads that file with `if: always()`, because the
run whose numbers someone actually needs to read is the red one. It deliberately
cannot name `results.json` — a job able to correct its own input cannot fail on
it, and `--report results.json` would be exactly that in a different spelling.

`--write-manifest` is a separate flag and **not** a consequence of `--write`.
If the manifest were rewritten alongside the results, the drift guard would
disarm itself on the first run after a chunker change: the shifted ordinals
would silently become the new manifest and the new normal, and the metrics
computed against them would look like an ordinary regression.

`--check` never writes `results.json`. A job that can correct its own input
cannot fail on it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.embedding import MODEL_NAME
from app.chat.retrieval import SearchMode, search_chunks
from app.config import get_settings
from app.db.session import tenant_session
from app.evals import goldset
from app.evals.corpus import (
    EVAL_ORG_ID,
    SeededChunk,
    _refuse_on_a_public_box,
    assert_corpus_matches_manifest,
    build_corpus,
    build_manifest,
    corpus_fingerprint,
    count_unnamed_chunks,
    load_manifest,
    seed_eval_corpus,
    write_manifest,
)
from app.evals.goldset import QUESTIONS, Question, goldset_sha256, referenced_keys
from app.evals.score import (
    ChunkKey,
    answerability_auc,
    mean_of_defined,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
)

_HERE = Path(__file__).resolve().parent
RESULTS_FILE = _HERE / "results.json"
FLOORS_FILE = _HERE / "floors.json"

SCHEMA = 1

# `limit=10`, not the default 5. `nDCG@10` and `MRR@10` need ten rows, and the
# panel's `precision@3` is computed from the same ten by slicing — so one search
# feeds every metric and the numbers for one question are internally consistent
# by construction rather than by discipline.
LIMIT = 10

MODES: tuple[SearchMode, ...] = (SearchMode.FTS, SearchMode.VECTOR, SearchMode.HYBRID)


@dataclass(frozen=True)
class QuestionResult:
    question_id: str
    kind: str
    retrieved: tuple[ChunkKey, ...]
    # The best score the mode gave this question, or 0.0 when it returned
    # nothing. Only `answerability_auc` reads it, and only through its ordering
    # against the other questions' — the value itself is not comparable between
    # modes and this file never treats it as if it were.
    top_score: float


@dataclass(frozen=True)
class ModeResult:
    mode: SearchMode
    precision_at_3: float | None
    recall_at_5: float | None
    mrr_at_10: float | None
    ndcg_at_10: float | None
    answerability_auc: float | None
    per_question: tuple[QuestionResult, ...]


def _aggregate(
    mode: SearchMode,
    questions: Sequence[Question],
    per_question: Sequence[QuestionResult],
) -> ModeResult:
    """Macro-average the four ranked metrics and compute the AUC over the fifth.

    `precision_at_3` averages over *all* thirty questions, the other three over
    the questions they are defined for. That asymmetry is deliberate and it is
    the whole design of `score.py`: precision is defined on an unanswerable
    question (it is 0, correctly, when the mode returned irrelevant rows), while
    recall, reciprocal rank and nDCG are not defined at all.
    """
    by_id = {question.id: question for question in questions}
    return ModeResult(
        mode=mode,
        precision_at_3=mean_of_defined(
            precision_at_k(result.retrieved, by_id[result.question_id].relevance, 3)
            for result in per_question
        ),
        recall_at_5=mean_of_defined(
            recall_at_k(result.retrieved, by_id[result.question_id].relevance, 5)
            for result in per_question
        ),
        mrr_at_10=mean_of_defined(
            reciprocal_rank(result.retrieved, by_id[result.question_id].relevance)
            for result in per_question
        ),
        ndcg_at_10=mean_of_defined(
            ndcg_at_k(result.retrieved, by_id[result.question_id].relevance, 10)
            for result in per_question
        ),
        answerability_auc=answerability_auc(
            [r.top_score for r in per_question if by_id[r.question_id].is_answerable],
            [r.top_score for r in per_question if not by_id[r.question_id].is_answerable],
        ),
        per_question=tuple(per_question),
    )


async def measure(
    session: AsyncSession,
    mode: SearchMode,
    chunks: Sequence[SeededChunk],
    questions: Sequence[Question] = QUESTIONS,
) -> ModeResult:
    """Every question through `search_chunks` under `tenant_session`. No mock, no shortcut.

    The signature is the one Task 20 settled — `search_chunks(session, query, *,
    mode, limit=5)` — and this call is the reason it had to be settled.

    One session for all thirty questions, opened once by the caller:
    `tenant_session` sets the GUC transaction-locally, so a session per question
    would be thirty round trips to set the same value.

    A `Citation` reports `chunk_id` and nothing else identifying, so the mapping
    from row id back to `(source_key, chunk_ordinal)` comes from the seeding
    itself. A chunk that came back but is not in the map would be a chunk this
    process did not write, which under row-level security cannot happen — so it
    raises rather than being scored as a miss.

    **The question embedding is computed inside `search_chunks`, once per
    (question, mode) rather than once per question.** The plan asked for one
    inference per question so that `vector` and `hybrid` provably share a vector;
    doing that here would mean reaching into the embedder and memoising it, which
    is the one shortcut in a function whose entire claim is that it takes the
    real path. Instead the property is checked directly, by
    `test_the_same_question_embeds_to_the_same_vector_twice_in_one_process`, and
    the cost of not memoising is 60 inferences instead of 30 — under two seconds
    against a reseed measured in minutes.
    """
    by_chunk_id = {str(chunk.chunk_id): chunk.key for chunk in chunks}
    results: list[QuestionResult] = []
    for question in questions:
        citations = await search_chunks(session, question.text, mode=mode, limit=LIMIT)
        retrieved: list[ChunkKey] = []
        for citation in citations:
            chunk_id = str(citation["chunk_id"])
            if chunk_id not in by_chunk_id:
                raise RuntimeError(
                    f"{mode} returned chunk {chunk_id} for {question.id}, which this run "
                    "did not seed — the eval organisation holds rows from somewhere else"
                )
            retrieved.append(by_chunk_id[chunk_id])
        results.append(
            QuestionResult(
                question_id=question.id,
                kind=question.kind,
                retrieved=tuple(retrieved),
                top_score=float(citations[0]["score"]) if citations else 0.0,
            )
        )
    return _aggregate(mode, questions, results)


def _by_kind(
    questions: Sequence[Question], per_question: Sequence[QuestionResult]
) -> dict[str, dict[str, float | None]]:
    """The same four ranked metrics, split by what each question exercises.

    A regression that reads "paraphrase recall fell from 0.55 to 0.20 and
    nothing else moved" points at a change; one that reads "MRR fell 0.04" points
    at nothing. This is the only reason `Question.kind` exists.
    """
    by_id = {question.id: question for question in questions}
    kinds = sorted({result.kind for result in per_question})
    out: dict[str, dict[str, float | None]] = {}
    for kind in kinds:
        subset = [result for result in per_question if result.kind == kind]
        out[kind] = {
            "questions": len(subset),
            "precision_at_3": mean_of_defined(
                precision_at_k(r.retrieved, by_id[r.question_id].relevance, 3) for r in subset
            ),
            "recall_at_5": mean_of_defined(
                recall_at_k(r.retrieved, by_id[r.question_id].relevance, 5) for r in subset
            ),
            "mrr_at_10": mean_of_defined(
                reciprocal_rank(r.retrieved, by_id[r.question_id].relevance) for r in subset
            ),
            "ndcg_at_10": mean_of_defined(
                ndcg_at_k(r.retrieved, by_id[r.question_id].relevance, 10) for r in subset
            ),
        }
    return out


def _commit() -> str:
    """The revision the measurement was taken at.

    `GITHUB_SHA` first, because in a CI checkout `git rev-parse HEAD` names the
    merge commit the runner built rather than the commit under test.
    """
    import os

    if sha := os.environ.get("GITHUB_SHA"):
        return sha
    try:
        # A fixed argument vector with no shell and no user input; ruff's S603
        # is about untrusted arguments and there are none here.
        return subprocess.run(  # noqa: S603
            ["/usr/bin/env", "git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=_HERE,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _results_document(
    chunks: Sequence[SeededChunk],
    questions: Sequence[Question],
    mode_results: Sequence[ModeResult],
) -> dict:
    sources: dict[str, int] = {}
    for chunk in chunks:
        sources[chunk.source_key] = sources.get(chunk.source_key, 0) + 1
    return {
        "schema": SCHEMA,
        "measured_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": _commit(),
        "provenance": {
            "goldset_sha256": goldset_sha256(questions),
            "corpus_fingerprint": corpus_fingerprint(chunks),
            "embedding_model": MODEL_NAME,
            "limit": LIMIT,
            "modes": [mode.value for mode in MODES],
        },
        "corpus": {"sources": dict(sorted(sources.items())), "chunks": len(chunks)},
        "questions": {
            "total": len(questions),
            "answerable": sum(1 for question in questions if question.is_answerable),
            "unanswerable": sum(1 for question in questions if not question.is_answerable),
        },
        "modes": {
            result.mode.value: {
                "precision_at_3": result.precision_at_3,
                "recall_at_5": result.recall_at_5,
                "mrr_at_10": result.mrr_at_10,
                "ndcg_at_10": result.ndcg_at_10,
                "answerability_auc": result.answerability_auc,
                "by_kind": _by_kind(questions, result.per_question),
            }
            for result in mode_results
        },
    }


async def _measure_everything() -> tuple[list[SeededChunk], list[ModeResult]]:
    """Check the manifest, reseed, measure every mode.

    The manifest is checked **before** the reseed, not after. Nothing is gained
    by spending a minute of ONNX inference on a corpus that is already known not
    to be the one the gold set was written against, and the error is clearer
    when nothing has been written.
    """
    settings = get_settings()
    _refuse_on_a_public_box(settings)

    planned = build_corpus()
    assert_corpus_matches_manifest(planned, load_manifest())

    async with tenant_session(EVAL_ORG_ID) as session:
        chunks = await seed_eval_corpus(session)

    async with tenant_session(EVAL_ORG_ID) as session:
        unnamed = await count_unnamed_chunks(session)
        if unnamed:
            raise RuntimeError(
                f"{unnamed} chunk(s) in the eval corpus carry no stable key and cannot be "
                "scored — a gold set keyed on (source_key, chunk_ordinal) would count every "
                "one of them as a miss"
            )
        results = [await measure(session, mode, chunks) for mode in MODES]
    return chunks, results


def _report(document: dict) -> None:
    print(f"corpus:    {document['corpus']['chunks']} chunks {document['corpus']['sources']}")
    print(
        f"questions: {document['questions']['total']} "
        f"({document['questions']['answerable']} answerable, "
        f"{document['questions']['unanswerable']} unanswerable)"
    )
    def cell(values: Mapping[str, object], name: str) -> str:
        value = values[name]
        return "n/a" if value is None else f"{value:.4f}"

    print(f"{'mode':8} {'P@3':>7} {'R@5':>7} {'MRR@10':>7} {'nDCG@10':>8} {'AUC':>7}")
    for mode, values in document["modes"].items():
        print(
            f"{mode:8} {cell(values, 'precision_at_3'):>7} {cell(values, 'recall_at_5'):>7} "
            f"{cell(values, 'mrr_at_10'):>7} {cell(values, 'ndcg_at_10'):>8} "
            f"{cell(values, 'answerability_auc'):>7}"
        )


def _load_floors() -> dict:
    return json.loads(FLOORS_FILE.read_text(encoding="utf-8"))


def _check_floors(document: dict) -> list[str]:
    """Compare the measurement to `floors.json`. Empty floors are a no-op, loudly.

    A threshold is an object rather than a bare number, and each field in it is
    load-bearing. `floor` is what gates. `measured` is the value the floor was
    derived from, so the headroom is visible in the file instead of having to be
    reconstructed from a commit message. `why` is one sentence on why *that*
    headroom, which is the question a reviewer has when the diff lowers a
    number. `provisional` records who has vouched for it — see the warning
    below.

    Empty floors report that they enforced nothing, rather than printing a
    reassuring silence that reads identically to a gate that passed.
    """
    floors = _load_floors()
    failures: list[str] = []
    configured = 0
    provisional = 0
    for mode, thresholds in floors.get("modes", {}).items():
        measured = document["modes"].get(mode)
        if measured is None:
            failures.append(f"floors.json names mode {mode!r}, which was not measured")
            continue
        for metric, threshold in thresholds.items():
            configured += 1
            floor = threshold["floor"]
            provisional += bool(threshold.get("provisional"))
            value = measured.get(metric)
            if value is None:
                failures.append(f"{mode}.{metric}: floor {floor}, but the metric is undefined")
            elif value < floor:
                failures.append(f"{mode}.{metric}: {value:.4f} < floor {floor}")
    if not configured:
        print("floors.json carries no thresholds yet — nothing was enforced")
    if provisional:
        # stderr and on every run, not once at the top of a log nobody scrolls
        # to. The promise this keeps is that the thirty gold-set questions get
        # human eyes before any number derived from them becomes a gate; the
        # gate ships anyway, and this line is what stops "provisional" from
        # decaying into "permanent" unnoticed. It does NOT soften the gate —
        # a provisional floor fails a build exactly as a reviewed one does.
        # The marker records who has vouched for the number, not how much it
        # is trusted.
        print(
            f"NOTE {provisional} of {configured} floors in {FLOORS_FILE.name} are PROVISIONAL: "
            f"set on {floors.get('set_on')} without the human review of the gold set they are "
            "derived from, and pending it. They gate exactly as a reviewed floor does.",
            file=sys.stderr,
        )
    return failures


def _annotate_metric_drift(document: dict) -> None:
    """Recomputed metrics against the committed ones: an observation, not a failure.

    A drift wider than `tolerance` means this machine and the one that wrote
    `results.json` disagree by more than the spread that was measured when the
    floors were set. That is worth a look and is not worth blocking a merge on:
    the thing that must not regress is already guarded by the floors, against
    numbers this same run produced from this same commit's code. Failing here
    instead would make a green change go red on a machine difference — the
    fastest known way to teach people that a red gate means "click re-run".

    `::warning file=…::` is GitHub's annotation syntax; outside Actions it is an
    ordinary line of output, which is the right thing for it to be.
    """
    committed = json.loads(RESULTS_FILE.read_text(encoding="utf-8")).get("modes", {})
    tolerance = float(_load_floors().get("tolerance", 0.0))
    for mode, values in document["modes"].items():
        was = committed.get(mode, {})
        for metric, recomputed in values.items():
            if metric == "by_kind" or recomputed is None:
                continue
            before = was.get(metric)
            if before is None or abs(float(before) - float(recomputed)) <= tolerance:
                continue
            print(
                f"::warning file=backend/app/evals/results.json::{mode}.{metric}: "
                f"committed {float(before):.4f}, recomputed {float(recomputed):.4f} "
                f"(tolerance {tolerance})"
            )


def _check_provenance(document: dict) -> list[str]:
    """The recorded run has to be about the gold set and corpus in this tree."""
    recorded = json.loads(RESULTS_FILE.read_text(encoding="utf-8")).get("provenance", {})
    failures: list[str] = []
    for field in ("goldset_sha256", "corpus_fingerprint", "embedding_model", "limit"):
        if recorded.get(field) != document["provenance"][field]:
            failures.append(
                f"provenance.{field}: results.json records {recorded.get(field)!r}, "
                f"this run computed {document['provenance'][field]!r} — "
                "re-run `python -m app.evals.run --write`"
            )
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.evals.run", description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="reseed, measure, write results.json")
    group.add_argument(
        "--write-manifest",
        action="store_true",
        help="rewrite manifest.json from the parsed corpus (no database, no model)",
    )
    group.add_argument(
        "--check", action="store_true", help="reseed, measure, compare to floors.json"
    )
    parser.add_argument(
        "--report",
        metavar="PATH",
        help="also write the recomputed document to PATH (never results.json)",
    )
    args = parser.parse_args(argv)

    if args.report and Path(args.report).resolve() == RESULTS_FILE:
        parser.error(
            "--report may not name results.json; that is what --write is for, and a "
            "--check able to rewrite its own input could not fail on it"
        )

    if args.write_manifest:
        if args.report:
            parser.error("--report has nothing to write under --write-manifest: no run was made")
        chunks = build_corpus()
        manifest = build_manifest(chunks, referenced_keys(goldset.QUESTIONS))
        write_manifest(manifest)
        print(
            f"manifest.json: {manifest.total} chunks across {len(manifest.sources)} source(s), "
            f"{len(manifest.chunks)} pinned by content"
        )
        return 0

    chunks, results = asyncio.run(_measure_everything())
    document = _results_document(chunks, QUESTIONS, results)
    _report(document)

    # Before anything that can fail, so the numbers behind a red gate survive it.
    if args.report:
        Path(args.report).write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.report}")

    if args.write:
        RESULTS_FILE.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"wrote {RESULTS_FILE}")
        return 0

    provenance = _check_provenance(document)
    # Only when the provenance matched. Comparing this run's metrics against
    # numbers measured on a different gold set or a different corpus would
    # annotate every cell and say nothing about any of them.
    if not provenance:
        _annotate_metric_drift(document)
    failures = provenance + _check_floors(document)
    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
