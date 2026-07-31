import structlog

from app.logging import configure_logging


def test_tracebacks_do_not_leak_frame_locals(capsys) -> None:
    """format_exc_info, not dict_tracebacks, must render exception tracebacks.

    dict_tracebacks records the local variables of every frame. In a service
    whose frames hold tenant document text (the ingestion path), that turns
    any parser exception into a cross-tenant content leak in the log. This
    pins the safer processor so a future swap back is caught here rather than
    in production logs.
    """
    configure_logging()
    logger = structlog.get_logger("test_logging")

    def _raise_with_sensitive_local() -> None:
        secret_document_text = "SENTINEL-DO-NOT-LOG"  # noqa: F841, S105
        raise ValueError("synthetic failure for the traceback test")

    try:
        _raise_with_sensitive_local()
    except ValueError:
        logger.exception("test_unhandled_exception")

    output = capsys.readouterr().out

    assert "ValueError" in output
    assert "synthetic failure for the traceback test" in output
    assert "SENTINEL-DO-NOT-LOG" not in output
