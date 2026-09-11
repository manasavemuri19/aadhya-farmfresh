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
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                for i in range(0, len(messages), _CHUNK_SIZE):
                    chunk = messages[i : i + _CHUNK_SIZE]
                    response = await client.post(
                        _EXPO_PUSH_URL,
                        json=chunk,
                        headers={"Accept": "application/json", "Content-Type": "application/json"},
                    )
                    response.raise_for_status()
                    tickets = response.json().get("data", [])
                    for token, ticket in zip((m["to"] for m in chunk), tickets, strict=False):
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
                            log.error(
                                "push notification ticket error",
                                extra={"error_code": error_code, "message": ticket.get("message")},
                            )
        except Exception:
            log.exception("push notification send failed")
            return

        log.info("push notifications sent", extra={"count": len(messages), "invalid": len(invalid)})
        if invalid:
            await self.tokens.delete_invalid(invalid)
