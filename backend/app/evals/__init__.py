"""Retrieval evaluation: a gold set, a scoring harness and a measurement.

Everything before this package claims the search is good. This package turns
that claim into five numbers a sceptical reader can recompute:

    python -m app.evals.run --write            # reseed, measure, rewrite results.json
    python -m app.evals.run --write-manifest   # reseed, rewrite manifest.json
    python -m app.evals.run --check            # reseed, measure, compare (CI)

The four artefacts and what each is for:

    goldset.py      thirty questions with graded relevance judgements, each
                    judgement carrying the reason it was made
    manifest.json   sha256 of every chunk the gold set names, plus a count per
                    source — the guard that turns a chunker change into a loud
                    error instead of a plausible-looking metric regression
    results.json    the measurement, plus the provenance needed to tell whether
                    it is still about the corpus and gold set in the tree
    floors.json     the thresholds a CI gate enforces; an empty skeleton here,
                    filled by Task 27 once it has measured the run-to-run spread

Nothing in this package is imported by the application. It reaches into
`app.chat.retrieval`, `app.ingest.parse` and `app.chat.corpus`, never the other
way round, so a broken eval cannot break the demo.
"""
