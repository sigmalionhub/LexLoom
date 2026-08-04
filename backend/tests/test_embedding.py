"""What the embedding provider must be true of, split by what it costs to check.

Two of these tests need the real 90 MB ONNX model on disk and carry
`@pytest.mark.model`; conftest skips them when the cache is empty, so a clean
CI runner does not download the model to learn that 384 is still 384. The rest
run against a stub and are the ones that would actually catch a regression in
the code written here — the loading discipline, the delegation, and the fact
that the request path does not run inference on the event loop.
"""

import asyncio
import subprocess
import sys

import pytest

from app.chat import embedding
from app.chat.embedding import DIMENSIONS, Embedder, get_embedder


@pytest.fixture
def stub_model(monkeypatch):
    """Replace the loaded model, not the loader.

    Patching `_load` would leave `_model` populated from an earlier test in the
    same process, so the stub would be ignored exactly when a previous test had
    already loaded the real thing. Setting the module global directly is the
    only assignment that is unconditional.
    """

    class _Stub:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        def embed(self, texts):
            self.calls.append(list(texts))
            # A numpy-free stand-in that still has `.tolist()`, because that is
            # the only thing `encode` uses of the real return value.
            return [_Row([float(len(t))] * DIMENSIONS) for t in texts]

    class _Row(list):
        def tolist(self):
            return list(self)

    stub = _Stub()
    monkeypatch.setattr(embedding, "_model", stub)
    yield stub
    monkeypatch.setattr(embedding, "_model", None)


@pytest.mark.model
def test_embedding_has_the_dimension_the_column_declares():
    # 384 is not a tuning knob: document_chunks.embedding is Vector(384), and a
    # model of any other width fails at INSERT with a dimension mismatch that
    # names neither the model nor the column.
    vec = get_embedder().encode_query("processor obligations")
    assert len(vec) == 384


@pytest.mark.model
def test_same_text_embeds_identically():
    # Not a property of embeddings in general — a property of THIS deployment.
    # The eval harness in M2.4 compares runs, and a model that drifts between
    # calls would make every comparison meaningless in a way that looks like a
    # retrieval regression.
    e = get_embedder()
    assert e.encode_query("erasure") == e.encode_query("erasure")


def test_the_declared_dimension_matches_the_database_column():
    """DIMENSIONS is the same number the migration wrote, checked against the
    ORM rather than against a second literal here.

    Runs without the model, so the mismatch that would fail every INSERT is
    caught on a bare CI runner rather than only where the model happens to be
    cached.
    """
    from app.db.models import DocumentChunk

    assert DocumentChunk.__table__.c.embedding.type.dim == DIMENSIONS


def test_importing_the_module_does_not_import_fastembed():
    """The loading discipline, asserted rather than trusted.

    Importing `app.chat.embedding` must not pull in ONNX Runtime. That is what
    keeps the Celery parent single-threaded, so the fork it performs is not the
    fork-from-a-multi-threaded-process that CPython warns may deadlock a child.

    Two stronger justifications are deliberately NOT claimed here, because
    neither reproduced when they were probed for task 19: the hang itself, and
    per-child memory duplication. See the docstring in `app/worker/signals.py`.
    What this test pins is the discipline, which is cheap and which a single
    stray top-level import would silently undo.

    In a subprocess because the assertion is about `sys.modules`, and any
    earlier test in this process may already have loaded fastembed for its own
    reasons — in-process this would pass or fail on test ordering.
    """
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import sys; import app.chat.embedding; "
            "assert 'fastembed' not in sys.modules, sorted(sys.modules); "
            "assert 'onnxruntime' not in sys.modules, sorted(sys.modules)",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_the_worker_loads_the_model_after_the_fork_not_at_import():
    """`signals.py` owns the load, and it must reach it through a deferred import.

    Reading the source rather than running a worker: the failure this guards is
    a hang, so a test that exercised it would hang too. What is checkable
    cheaply is the shape — that the module-level imports of
    `app/worker/signals.py` do not name the embedder, and that the
    `worker_process_init` handler does.
    """
    import ast
    import inspect
    from pathlib import Path

    from app.worker import signals

    source = Path(inspect.getsourcefile(signals)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    module_level_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    named = {
        alias.name for node in module_level_imports if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or "" for node in module_level_imports
        if isinstance(node, ast.ImportFrom)
    }
    assert not any("embedding" in name or "fastembed" in name for name in named), (
        f"signals.py imports the embedder at module level ({sorted(named)}) — "
        "that builds the model in the Celery parent, which makes the parent "
        "multi-threaded before it forks"
    )

    handler = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "on_worker_process_init"
    )
    assert "app.chat.embedding" in ast.dump(handler), (
        "the worker_process_init handler does not load the embedder — the "
        "worker would load it lazily on the first task instead, inside a child "
        "that has already forked but under the task's own time limit"
    )


def test_get_embedder_returns_one_instance_per_process():
    assert get_embedder() is get_embedder()


def test_encode_returns_plain_lists_not_arrays(stub_model):
    """The value goes to asyncpg and to JSON. A numpy array reaches neither."""
    vectors = Embedder().encode(["a", "bb"])
    assert [type(v) for v in vectors] == [list, list]
    assert all(isinstance(x, float) for x in vectors[0])
    assert len(vectors) == 2


def test_encode_query_embeds_one_text_and_unwraps_it(stub_model):
    vector = Embedder().encode_query("erasure")
    assert stub_model.calls == [["erasure"]]
    assert len(vector) == DIMENSIONS
    assert not isinstance(vector[0], list)


def test_the_model_is_loaded_once_even_under_concurrent_first_calls(monkeypatch):
    """Two threads racing `_ensure` must not build two InferenceSessions.

    Not a tidiness point on a 2 vCPU box: a second session is a second ~180 MiB
    of private memory in one process, and the container limit is sized for one.
    """
    monkeypatch.setattr(embedding, "_model", None)
    loads = []

    def _slow_load():
        loads.append(1)
        # Long enough that a second thread reaches the check while this one is
        # still inside the lock; without the lock this test fails ~always.
        import time

        time.sleep(0.05)
        return object()

    monkeypatch.setattr(embedding, "_load", _slow_load)

    import threading

    embedder = Embedder()
    threads = [threading.Thread(target=embedder._ensure) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(loads) == 1
    monkeypatch.setattr(embedding, "_model", None)


async def test_aencode_query_does_not_run_inference_on_the_event_loop(stub_model):
    """The request path must hand inference to a thread.

    ONNX inference holds the GIL for tens of milliseconds; on the event loop
    that stalls every concurrent SSE stream for exactly that long, which is the
    bug this whole service exists to not have.
    """
    loop_thread = __import__("threading").get_ident()
    seen: list[int] = []

    original = Embedder.encode_query

    def _record(self, text):
        seen.append(__import__("threading").get_ident())
        return original(self, text)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Embedder, "encode_query", _record)
        await Embedder().aencode_query("erasure")

    assert seen and seen[0] != loop_thread, "inference ran on the event loop thread"


async def test_the_request_path_admits_only_two_concurrent_inferences(monkeypatch):
    """The semaphore, not the thread pool size, is what bounds this.

    `asyncio.to_thread` uses the default executor, sized from the HOST's CPU
    count rather than the container's, so with MAX_CONCURRENT_STREAMS=16 an
    unbounded path would put sixteen ONNX inferences on two shared vCPUs.

    The module-level semaphore is replaced with one created inside this test's
    event loop: each test here gets its own loop, and an `asyncio.Semaphore`
    binds to the first loop that has to *wait* on it. Contending with the
    module-level object would bind it to this test's loop and make a later
    test's contention raise "bound to a different event loop".
    """
    monkeypatch.setattr(embedding, "_INFERENCE", asyncio.Semaphore(2))

    import threading

    guard = threading.Lock()
    release = threading.Event()
    concurrent = 0
    peak = 0

    def _blocking_encode(self, text):
        nonlocal concurrent, peak
        with guard:
            concurrent += 1
            peak = max(peak, concurrent)
        release.wait(timeout=5)
        with guard:
            concurrent -= 1
        return [0.0] * DIMENSIONS

    monkeypatch.setattr(Embedder, "encode_query", _blocking_encode)

    embedder = Embedder()
    tasks = [asyncio.create_task(embedder.aencode_query("q")) for _ in range(8)]
    # Long enough for every admitted call to have reached the counter, short
    # enough to stay well inside the per-test timeout.
    await asyncio.sleep(0.3)
    with guard:
        observed_peak = peak
    release.set()
    await asyncio.gather(*tasks)

    assert observed_peak == 2, (
        f"{observed_peak} inferences ran at once; the semaphore admits 2. More "
        "concurrency on two shared cores buys queueing, not throughput."
    )
