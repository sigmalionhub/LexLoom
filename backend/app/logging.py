import contextvars
import logging

import structlog

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
org_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("org_id", default="")


def _bind_context(_: object, __: str, event_dict: dict) -> dict:
    if rid := request_id_var.get():
        event_dict["request_id"] = rid
    if oid := org_id_var.get():
        event_dict["org_id"] = oid
    return event_dict


def configure_logging() -> None:
    logging.basicConfig(format="%(message)s", level=logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _bind_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            # Without this, logger.exception() serialises as the bare boolean
            # `"exc_info": true` and the traceback is lost — the one thing the
            # generic 500 response tells the caller to go and look for.
            #
            # format_exc_info, not dict_tracebacks: the latter records the
            # local variables of every frame. In a service whose frames hold
            # tenant document text, that turns any parser exception into a
            # cross-tenant content leak in the log.
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        cache_logger_on_first_use=True,
    )
