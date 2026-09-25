"""AAD-QUAL-032 (a truncated Expo response is silently tolerated) and
AAD-QUAL-033 (a later chunk's failure discards invalid tokens an earlier,
successful chunk already found) — PushService._send had no test coverage
of any kind before this batch.
"""

from __future__ import annotations

import logging

import pytest

import app.services.push_service as push_service_module
from app.repositories.push_tokens import PushTokenRepository
from app.services.push_service import PushService


class _FakeResponse:
    def __init__(self, *, json_data=None):
        self._json = json_data or {}

    def raise_for_status(self):
        return None

    def json(self):
        return self._json


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient: each call to `post` pops the next
    scripted response (or raises it, if it's an exception instance)."""

    def __init__(self, responses, **_kw):
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, *, json, headers):
        self.calls.append(json)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeTokens:
    """A bare stand-in for PushTokenRepository, used where the test cares
    only about what _send does with it, not real persistence."""

    def __init__(self):
        self.deleted: list[str] = []

    async def delete_invalid(self, tokens: list[str]) -> None:
        self.deleted.extend(tokens)


def _patch_client(monkeypatch, responses):
    fake = _FakeAsyncClient(responses)
    monkeypatch.setattr(push_service_module.httpx, "AsyncClient", lambda **kw: fake)
    return fake


async def test_device_not_registered_tickets_are_pruned(monkeypatch):
    _patch_client(monkeypatch, [
        _FakeResponse(json_data={"data": [
            {"status": "error", "details": {"error": "DeviceNotRegistered"}},
        ]}),
    ])
    tokens = _FakeTokens()
    await PushService(tokens)._send(["tok_dead"], title="t", body="b", data=None)
    assert tokens.deleted == ["tok_dead"]


async def test_other_ticket_errors_are_logged_but_not_pruned(monkeypatch, caplog):
    _patch_client(monkeypatch, [
        _FakeResponse(json_data={"data": [
            {"status": "error", "details": {"error": "MessageRateExceeded"}},
        ]}),
    ])
    tokens = _FakeTokens()
    with caplog.at_level(logging.ERROR):
        await PushService(tokens)._send(["tok_ok"], title="t", body="b", data=None)
    assert tokens.deleted == []
    assert any("ticket error" in r.message for r in caplog.records)


# --- AAD-QUAL-032: a short ticket list is logged, not silently accepted ---


async def test_fewer_tickets_than_messages_is_logged(monkeypatch, caplog):
    _patch_client(monkeypatch, [
        # Two messages sent, only one ticket comes back.
        _FakeResponse(json_data={"data": [{"status": "ok"}]}),
    ])
    tokens = _FakeTokens()
    with caplog.at_level(logging.ERROR):
        await PushService(tokens)._send(
            ["tok_a", "tok_b"], title="t", body="b", data=None
        )
    assert any(
        "different number of tickets" in r.message for r in caplog.records
    )


async def test_matching_ticket_count_is_not_logged_as_a_mismatch(monkeypatch, caplog):
    _patch_client(monkeypatch, [
        _FakeResponse(json_data={"data": [{"status": "ok"}, {"status": "ok"}]}),
    ])
    tokens = _FakeTokens()
    with caplog.at_level(logging.ERROR):
        await PushService(tokens)._send(
            ["tok_a", "tok_b"], title="t", body="b", data=None
        )
    assert not any(
        "different number of tickets" in r.message for r in caplog.records
    )


# --- AAD-QUAL-033: a later chunk's failure doesn't lose an earlier chunk's
# already-identified invalid tokens ---


async def test_a_failed_later_chunk_does_not_discard_an_earlier_chunks_invalid_tokens(
    monkeypatch,
):
    monkeypatch.setattr(push_service_module, "_CHUNK_SIZE", 1)
    _patch_client(monkeypatch, [
        # Chunk 1: tok_dead is reported DeviceNotRegistered.
        _FakeResponse(json_data={"data": [
            {"status": "error", "details": {"error": "DeviceNotRegistered"}},
        ]}),
        # Chunk 2: the network call itself blows up.
        RuntimeError("connection reset"),
    ])
    tokens = _FakeTokens()
    await PushService(tokens)._send(
        ["tok_dead", "tok_other"], title="t", body="b", data=None
    )
    # Before this fix, the chunk-2 exception hit `return` before
    # delete_invalid ever ran, so tok_dead (found in chunk 1) was lost.
    assert tokens.deleted == ["tok_dead"]


async def test_a_failed_chunk_is_logged_and_does_not_raise(monkeypatch, caplog):
    monkeypatch.setattr(push_service_module, "_CHUNK_SIZE", 1)
    _patch_client(monkeypatch, [
        RuntimeError("boom"),
        _FakeResponse(json_data={"data": [{"status": "ok"}]}),
    ])
    tokens = _FakeTokens()
    with caplog.at_level(logging.ERROR):
        await PushService(tokens)._send(
            ["tok_a", "tok_b"], title="t", body="b", data=None
        )
    assert any("failed for one chunk" in r.message for r in caplog.records)


# --- Integration: notify_users really deletes from the real table ---


async def test_notify_users_prunes_a_dead_token_from_the_real_table(
    monkeypatch, session, user
):
    push_tokens = PushTokenRepository(session)
    await push_tokens.register(user_id=user["id"], token="tok_real_dead", platform="android")
    await session.flush()

    _patch_client(monkeypatch, [
        _FakeResponse(json_data={"data": [
            {"status": "error", "details": {"error": "DeviceNotRegistered"}},
        ]}),
    ])
    await PushService(push_tokens).notify_users(
        [user["id"]], title="t", body="b",
    )

    remaining = await push_tokens.list_for_users([user["id"]])
    assert remaining == []
