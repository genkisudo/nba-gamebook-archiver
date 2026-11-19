"""Keep IA_ACCESS / IA_SECRET out of log lines.

Logger filters do not run for records that propagate from a child logger, so
the scrub happens when the LogRecord is built.
"""

from __future__ import annotations

import logging
import os

_INSTALLED = False
_PREVIOUS_FACTORY = None


def _scrub_record(record: logging.LogRecord) -> logging.LogRecord:
    secrets = [value for value in (os.environ.get("IA_ACCESS"), os.environ.get("IA_SECRET")) if value]
    if not secrets:
        return record
    if isinstance(record.msg, str):
        record.msg = _scrub(record.msg, secrets)
    record.args = _scrub_args(record.args, secrets)
    return record


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        _scrub_record(record)
        return True


def _scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "[redacted]")
    return text


def _scrub_args(args, secrets: list[str]):
    if isinstance(args, dict):
        return {key: _scrub(val, secrets) if isinstance(val, str) else val for key, val in args.items()}
    if isinstance(args, tuple):
        return tuple(_scrub(val, secrets) if isinstance(val, str) else val for val in args)
    return args


def install_redaction() -> None:
    global _INSTALLED, _PREVIOUS_FACTORY
    if _INSTALLED:
        return
    _PREVIOUS_FACTORY = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = _PREVIOUS_FACTORY(*args, **kwargs)
        return _scrub_record(record)

    logging.setLogRecordFactory(factory)
    logging.getLogger().addFilter(RedactFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _INSTALLED = True
