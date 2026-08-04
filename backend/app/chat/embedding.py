"""The embedding model, loaded exactly once per process and never at import.

Two processes need it and they need it differently. The API embeds one question
at a time on the request path; the worker embeds hundreds of chunks in a batch.
Both are CPU-bound ONNX inference, and both constraints below exist because of
where that CPU time is spent.
"""

from __future__ import annotations

import asyncio
import os
import threading
from functools import lru_cache
from pathlib import Path

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

# Not a tuning knob. `document_chunks.embedding` is `Vector(384)` (migration
# 0002), so a model of any other width fails at INSERT with a dimension
# mismatch that names neither the model nor the column.
DIMENSIONS = 384

# Where the ONNX weights live on disk. Resolved here rather than left to
# fastembed's own default, which is `$TMPDIR/fastembed_cache` — a 90 MB
# download into a directory the box may clear on reboot, and one that does not
# match what CI restores. Three deployments have to agree on this path or the
# model is fetched from HuggingFace at the worst possible moment:
#
#   image   FASTEMBED_CACHE_PATH is set in backend/Dockerfile, and the same
#           build step populates it, so the runtime never reaches the network.
#   CI      unset, so this default applies and it is exactly the path
#           `actions/cache` restores in .github/workflows/ci.yml.
#   dev     unset, same default, so a developer downloads once and not once
#           per checkout.
_DEFAULT_CACHE_DIR = Path.home() / ".cache" / "fastembed"

# A semaphore, not a bigger thread pool. The box has two cores and one of them
# is already someone else's; more concurrency here buys queueing, not
# throughput. `asyncio.to_thread` uses the default executor, whose size is
# computed from the HOST's CPU count rather than the container's, so with
# MAX_CONCURRENT_STREAMS=16 an unbounded path would put up to sixteen ONNX
# inferences on those two shared vCPUs. Ingest got careful admission control in
# Task 23 and the path a visitor hits on every single question got none — this
# closes that gap.
#
# Module level, so it is one budget for the whole process rather than one per
# caller. That is safe for a single-loop server (uvicorn --workers 1) because
# an asyncio.Semaphore binds to the first event loop that has to *wait* on it;
# tests that contend with it substitute their own, see test_embedding.py.
_INFERENCE = asyncio.Semaphore(2)

_lock = threading.Lock()
_model = None


def cache_dir() -> Path:
    """The directory the weights are read from and written to."""
    configured = os.environ.get("FASTEMBED_CACHE_PATH")
    return Path(configured) if configured else _DEFAULT_CACHE_DIR


def model_is_cached() -> bool:
    """True when the weights are already on disk, so nothing will hit the network.

    Asked by the Docker build (to prove the download landed where the runtime
    will look, rather than in a stage that gets discarded) and by conftest (to
    skip the tests that need the real model instead of downloading 90 MB on a
    bare CI runner).

    Looks for any `.onnx` file rather than for a directory name: fastembed's
    layout under the cache root is an implementation detail that has changed
    between releases, while "there is a compiled model here" has not.
    """
    directory = cache_dir()
    return directory.is_dir() and any(directory.rglob("*.onnx"))


def _load():
    # Imported here, not at module top, and the worker builds the model from
    # `worker_process_init` — i.e. after the fork. Two reasons, one measured and
    # one precautionary; they are recorded separately because they are not
    # equally certain.
    #
    # Measured: a model built in the Celery parent is inherited by every child.
    # fork() shares those pages copy-on-write, but ONNX Runtime writes into its
    # own arena on the first inference, so each child ends up paying for its own
    # copy of ~216 MiB anyway — on a 2 GB box shared with someone else's
    # production, at an unpredictable moment rather than at start-up.
    #
    # Precautionary: fork() copies a thread pool's bookkeeping but not its
    # threads, which is the classic way a child inherits a pool it can never
    # use and blocks on first use with no error and no traceback. That is what
    # the docstring in app/worker/signals.py warns about. It did NOT reproduce
    # here — with fastembed 0.5.1 a parent-built session survived the fork and
    # embedded correctly in the child, at OMP_NUM_THREADS=1 and at 4 (see the
    # task 19 report for the probe). The discipline is kept anyway: it costs
    # nothing, the memory reason above stands on its own, and "this version of
    # ONNX Runtime happens to tolerate fork" is not a property to build on.
    from fastembed import TextEmbedding

    # `cache_dir` passed explicitly rather than relying on fastembed reading
    # FASTEMBED_CACHE_PATH itself: that lookup is internal to fastembed and has
    # no test here, and getting it wrong is silent — the model downloads again,
    # to somewhere else, and everything still works until the box is offline.
    return TextEmbedding(model_name=MODEL_NAME, cache_dir=str(cache_dir()))


class Embedder:
    """A handle on the process-wide model.

    Cheap to construct and safe to hold; the model itself is the module-level
    `_model`, so two handles never mean two InferenceSessions — which on this
    box would mean two resident copies of ~216 MiB against a container limit
    sized for one.
    """

    def _ensure(self):
        global _model
        if _model is None:
            with _lock:
                if _model is None:
                    _model = _load()
        return _model

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch. Batch size is the caller's decision and it is a memory
        decision: measured resident cost is flat to ~8 texts, 253 MiB at 16,
        315 MiB at 32 and 444 MiB at 64. See the report for task 19.
        """
        return [v.tolist() for v in self._ensure().embed(texts)]

    def encode_query(self, text: str) -> list[float]:
        return self.encode([text])[0]

    async def aencode_query(self, text: str) -> list[float]:
        # to_thread, not a direct call. ONNX inference holds the GIL for tens of
        # milliseconds; on the event loop that stalls every concurrent SSE stream
        # for exactly that long. The whole point of this service is that tokens
        # arrive incrementally, and a synchronous embed on the request path is
        # the cheapest way to reintroduce the bug it exists to fix.
        async with _INFERENCE:
            return await asyncio.to_thread(self.encode_query, text)


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    return Embedder()
