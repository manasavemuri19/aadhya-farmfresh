"""Push notifications via Expo's push service.

Expo's HTTP API (`https://exp.host/--/api/v2/push/send`) fans a request out
to FCM (and APNs, if iOS ever ships) on our behalf — this is why the backend
only ever needs an *Expo* push token and never talks to Firebase directly.
A single request can carry up to 100 messages; chunking below is defensive
headroom, not a limit we're anywhere near yet.

Failures are logged and swallowed, never raised: a notification that didn't
arrive must never break the order or delivery-status update that triggered
it — see every call site in order_service.py. The one failure worth acting
on is Expo's "DeviceNotRegistered" ticket error, which means a token is
permanently dead (app uninstalled, token rotated); those get pruned so we
stop wasting sends on them.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.repositories.push_tokens import PushTokenRepository

log = logging.getLogger(__name__)

_EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_CHUNK_SIZE = 100


class PushService:
    def __init__(self, tokens: PushTokenRepository) -> None:
        self.tokens = tokens

    async def notify_users(
        self,
        user_ids: list[str],
        *,
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        tokens = await self.tokens.list_for_users(user_ids)
        if tokens:
            await self._send(tokens, title=title, body=body, data=data)

    async def _send(
        self, tokens: list[str], *, title: str, body: str, data: dict[str, Any] | None
    ) -> None:
        messages = [
            {"to": token, "title": title, "body": body, "data": data or {}, "sound": "default"}
            for token in tokens
        ]
        invalid: list[str] = []
        chunks_failed = 0
        async with httpx.AsyncClient(timeout=10) as client:
            for i in range(0, len(messages), _CHUNK_SIZE):
                chunk = messages[i : i + _CHUNK_SIZE]
                chunk_tokens = [m["to"] for m in chunk]
                # AAD-QUAL-033: the try/except used to wrap this whole
                # per-chunk loop, so a failure on chunk 2 of 2 hit `return`
                # before `delete_invalid(invalid)` ever ran — discarding
                # every `DeviceNotRegistered` token chunk 1 had already
                # identified, forever (they're never pruned again unless
                # that exact chunk happens to fail differently next time).
                # Scoped to one chunk now: a bad chunk is logged and
                # skipped, `invalid` keeps accumulating across the chunks
                # that do succeed, and `delete_invalid` always runs once at
                # the end over whatever was actually found.
                try:
                    response = await client.post(
                        _EXPO_PUSH_URL,
                        json=chunk,
                        headers={"Accept": "application/json", "Content-Type": "application/json"},
                    )
                    response.raise_for_status()
                    tickets = response.json().get("data", [])
                except Exception:
                    chunks_failed += 1
                    log.exception(
                        "push notification send failed for one chunk",
                        extra={"chunk_size": len(chunk)},
                    )
                    continue

                # AAD-QUAL-032: `zip(..., strict=False)` truncates silently
                # to the shorter of the two sequences. Expo's contract is
                # one ticket per message, in order — if that ever doesn't
                # hold (a partial response, a provider-side bug), the
                # tokens past the shorter length get no ticket to check at
                # all, including a possible `DeviceNotRegistered` among
                # them, and nothing said so. Still `strict=False` (a hard
                # `ValueError` here would be worse than a partial result),
                # but now logged so a genuine mismatch is visible instead
                # of indistinguishable from every ticket coming back clean.
                if len(tickets) != len(chunk_tokens):
                    log.error(
                        "expo returned a different number of tickets than "
                        "messages sent",
                        extra={"sent": len(chunk_tokens), "received": len(tickets)},
                    )
                for token, ticket in zip(chunk_tokens, tickets, strict=False):
                    if not isinstance(ticket, dict) or ticket.get("status") != "error":
                        continue
                    error_code = ticket.get("details", {}).get("error")
                    if error_code == "DeviceNotRegistered":
                        invalid.append(token)
                    else:
                        # Anything else (bad FCM credentials, a
                        # misconfigured project, a malformed message) was
                        # previously dropped on the floor here — silently
                        # indistinguishable from a working send. Logging
                        # it is what actually surfaces a broken FCM V1
                        # credential upload instead of the app just never
                        # notifying anyone with no trace of why.
                        #
                        # AAD-QUAL-032/033 side effect: this `extra` dict
                        # used to use the key "message", which collides
                        # with `LogRecord`'s own reserved `message`
                        # attribute — `logging` raises `KeyError` from
                        # inside `log.error()` itself the moment this line
                        # actually runs with a non-empty ticket message.
                        # Previously invisible: this whole block sat inside
                        # the try/except that wrapped the entire per-chunk
                        # loop, so the KeyError was caught and swallowed by
                        # the same handler as a real network failure, with
                        # no distinguishing trace. Writing this batch's own
                        # regression test — the first test coverage this
                        # file has ever had — is what surfaced it: once the
                        # try/except was narrowed to just the network call
                        # (see above), this KeyError started propagating
                        # for real instead of being silently absorbed.
                        # Renamed the key; a raised `KeyError` here would
                        # have taken down whatever fire-and-forget call site
                        # in order_service.py triggered the notification.
                        log.error(
                            "push notification ticket error",
                            extra={
                                "error_code": error_code,
                                "ticket_message": ticket.get("message"),
                            },
                        )

        log.info(
            "push notifications sent",
            extra={"count": len(messages), "invalid": len(invalid), "chunks_failed": chunks_failed},
        )
        if invalid:
            await self.tokens.delete_invalid(invalid)
