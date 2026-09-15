"""Structured JSON logging with request correlation.

Every log line carries the request id, so a single customer complaint can be
traced end to end from the mobile client through to the payment webhook.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from typing import Any

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
user_id_var: ContextVar[str] = ContextVar("user_id", default="-")

_RESERVED = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"asctime", "message", "taskName"}

# AAD-SEC-007: anything any caller ever passes as `extra=` used to be written
# verbatim to stdout, forever, with no allowlist, no denylist and no
# redaction. The existing discipline in this codebase — auth_service logging
# `phone_suffix` rather than the phone — is real, but it's enforced by
# nothing except the author remembering. One `log.info("order created",
# extra={"order": order_dict})` during a debugging session would permanently
# write customer names, phone numbers, full delivery addresses and GPS
# coordinates into whatever aggregates this stdout stream.
#
# A denylist over an allowlist: an allowlist would need every existing call
# site updated before this could ship without breaking logging outright, and
# would need updating again every time a new `extra=` key is added anywhere
# in the app — the audit's own note that this is "the stricter, right call
# for a codebase that will grow" is taken as a follow-up, not a blocker for
# closing this finding now. This denylist walks recursively — not just the
# top-level `extra` keys — because the exact scenario the finding itself
# describes, `extra={"order": order_dict}`, hides the sensitive fields one
# level down, under a key ("order") that isn't itself sensitive.
_DENYLIST_KEYS = frozenset(
    {
        "phone", "email", "address", "line1", "line2", "landmark",
        "lat", "latitude", "lng", "longitude",
        "token", "access_token", "refresh_token", "id_token",
        "secret", "authorization", "code", "otp", "card",
    }
)
_MAX_VALUE_CHARS = 512
_REDACTED = "***redacted***"
_MAX_REDACT_DEPTH = 4  # generous for any real log payload; stops a pathological structure


def _redact(key: str, value: Any, depth: int = 0) -> Any:
    if isinstance(key, str) and key.lower() in _DENYLIST_KEYS:
        return _REDACTED
    if depth >= _MAX_REDACT_DEPTH:
        return value
    if isinstance(value, dict):
        return {k: _redact(k, v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(key, v, depth + 1) for v in value]
    if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
        return value[:_MAX_VALUE_CHARS] + f"...({len(value)} chars, truncated)"
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
            "user_id": user_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = _redact(key, value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Uvicorn ships its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True

    # SQLAlchemy echoes every statement at INFO; that belongs behind SQL_ECHO.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)
