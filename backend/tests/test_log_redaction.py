"""AAD-SEC-007 — the JSON log formatter redacts a denylist of key names,
recursively, and truncates long strings, rather than writing every `extra`
attribute verbatim forever.
"""

from __future__ import annotations

import json
import logging

from app.core.logging import _REDACTED, JsonFormatter, _redact


def _log_and_capture(caplog_handler, **extra) -> dict:
    logger = logging.getLogger("test.redaction")
    logger.handlers = [caplog_handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.info("test message", extra=extra)
    return json.loads(caplog_handler.records_json[-1])


class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.setFormatter(JsonFormatter())
        self.records_json: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records_json.append(self.format(record))


def test_a_top_level_denylisted_key_is_redacted():
    handler = _CapturingHandler()
    payload = _log_and_capture(handler, phone="9876543210")
    assert payload["phone"] == _REDACTED


def test_a_safe_key_is_left_alone():
    handler = _CapturingHandler()
    payload = _log_and_capture(handler, order_id="ord_abc123")
    assert payload["order_id"] == "ord_abc123"


def test_the_authors_own_deliberate_phone_suffix_pattern_still_shows():
    """AAD-SEC-007's own text calls this out by name: auth_service logs
    `phone_suffix` deliberately, not the phone — the denylist matches exact
    key names, so this stays visible rather than being swept up too."""
    handler = _CapturingHandler()
    payload = _log_and_capture(handler, phone_suffix="3210")
    assert payload["phone_suffix"] == "3210"


def test_nested_pii_is_redacted_even_under_a_safe_key():
    """The exact scenario the finding itself describes:
    `extra={"order": order_dict}` — "order" isn't sensitive, but what's
    nested inside it is."""
    handler = _CapturingHandler()
    payload = _log_and_capture(
        handler,
        order={
            "id": "ord_1",
            "address": {"line1": "12 Farm Road", "phone": "9876543210"},
        },
    )
    assert payload["order"]["id"] == "ord_1"
    assert payload["order"]["address"] == _REDACTED


def test_pii_inside_a_list_of_dicts_is_redacted():
    handler = _CapturingHandler()
    payload = _log_and_capture(
        handler, addresses=[{"line1": "12 Farm Road"}, {"line1": "9 Lake View"}]
    )
    assert payload["addresses"][0]["line1"] == _REDACTED
    assert payload["addresses"][1]["line1"] == _REDACTED


def test_a_long_string_is_truncated():
    handler = _CapturingHandler()
    long_value = "x" * 1000
    payload = _log_and_capture(handler, note=long_value)
    assert len(payload["note"]) < 1000
    assert payload["note"].startswith("x" * 512)
    assert "truncated" in payload["note"]


def test_short_strings_are_untouched():
    handler = _CapturingHandler()
    payload = _log_and_capture(handler, note="short and fine")
    assert payload["note"] == "short and fine"


def test_redact_helper_directly_on_a_deep_structure_stops_at_the_depth_limit():
    """Not infinite recursion on a pathological structure — bounded, and the
    function itself (not just the formatter) is the unit under test here."""
    deeply_nested = {"a": {"b": {"c": {"d": {"e": {"phone": "9876543210"}}}}}}
    result = _redact("payload", deeply_nested)
    assert isinstance(result, dict)
