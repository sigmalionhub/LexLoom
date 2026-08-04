"""Precompute the seed corpus's embeddings into a file that is committed.

Run it from `backend/`, by hand, whenever `seed/gdpr_excerpts.json` or the model
changes:

    python scripts/embed_seed_corpus.py

Not at runtime and not in CI. `copy_corpus_into` runs on every demo login,
inside `provision_demo_org`'s open transaction, on an API that runs
`--workers 1`; thirty synchronous ONNX inferences there would hold the
transaction open and pause every concurrent SSE stream for the 0.6-2 s they cost
on a shared vCPU. And CI has no model — making it fetch 90 MB to recompute
thirty vectors that never change is the other half of the same mistake.

The output is checked in for two reasons beyond speed. It is small (thirty
vectors at six decimal places, ~100 KB), and it makes every tenant's copy of an
excerpt carry the *same* vector, byte for byte — which is what turns the M2.4
eval numbers into a measurement of retrieval rather than of which machine last
ran the model.

`corpus_sha256` is what keeps the two files honest: `load_seed_vectors` recomputes
it and refuses to serve vectors that describe some earlier text. Without it,
editing the corpus and forgetting this script is silent — retrieval keeps working
and keeps being wrong.
"""

import hashlib
import json
import pathlib
import sys

# `backend/` on the path so `app` imports when this is run as a script from
# anywhere, exactly as `seed_corpus.py` does it and for the same reason.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.chat.corpus import SEED_FILE, SEED_VECTORS_FILE, load_excerpts  # noqa: E402
from app.chat.embedding import DIMENSIONS, MODEL_NAME, get_embedder  # noqa: E402

# The ceiling measured in task 19, not a default to raise on a hunch: resident
# cost is flat to ~8 texts, 253 MiB at 16, 315 MiB at 32 and 444 MiB at 64,
# against a 650m worker limit. Thirty excerpts fit in one batch today; the loop
# is here so a grown corpus does not quietly become a single 600-wide batch.
BATCH = 32

# Six decimal places. The vectors are unit-normalised, so this is a ~5e-7
# per-component error — far below anything cosine similarity can distinguish —
# and it is the difference between a 100 KB file and a 230 KB one whose diffs
# are pure float noise.
DECIMALS = 6


def main() -> None:
    excerpts = load_excerpts()
    embedder = get_embedder()

    vectors: list[list[float]] = []
    for start in range(0, len(excerpts), BATCH):
        batch = [excerpt.content for excerpt in excerpts[start : start + BATCH]]
        vectors.extend(embedder.encode(batch))

    for index, vector in enumerate(vectors):
        if len(vector) != DIMENSIONS:
            raise SystemExit(
                f"{MODEL_NAME} returned a {len(vector)}-dimensional vector for excerpt "
                f"{index}, but document_chunks.embedding is vector({DIMENSIONS})"
            )

    rounded = [[round(value, DECIMALS) for value in vector] for vector in vectors]

    # Rendered by hand rather than with `indent=`, which would put all 11,520
    # floats on their own lines. One line per vector means a changed excerpt
    # shows up as one changed line in the diff, in the same order as the file it
    # came from — and that order is the key `(SEED_SOURCE_KEY, chunk_ordinal)`
    # is built from.
    body = ",\n    ".join(json.dumps(vector) for vector in rounded)
    document = (
        "{\n"
        f'  "model": {json.dumps(MODEL_NAME)},\n'
        f'  "dim": {DIMENSIONS},\n'
        f'  "corpus_sha256": {json.dumps(hashlib.sha256(SEED_FILE.read_bytes()).hexdigest())},\n'
        '  "vectors": [\n'
        f"    {body}\n"
        "  ]\n"
        "}\n"
    )
    SEED_VECTORS_FILE.write_text(document, encoding="utf-8")

    print(
        f"Wrote {len(rounded)} {DIMENSIONS}-dimensional vectors for {SEED_FILE.name} "
        f"to {SEED_VECTORS_FILE} using {MODEL_NAME}."
    )


if __name__ == "__main__":
    main()
