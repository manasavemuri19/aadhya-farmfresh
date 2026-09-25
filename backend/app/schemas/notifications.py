from __future__ import annotations

from pydantic import Field

from app.schemas.common import Schema

# AAD-SEC-031: `min_length=8` accepted literally any string 8+ characters
# long as a "push token" — every real Expo push token is shaped
# `ExponentPushToken[...]` (or the older `ExpoPushToken[...]`), so this at
# least rejects garbage before it's ever stored or handed to Expo's API.
# It does NOT close the underlying finding: anyone who *has* learned a
# victim's real, correctly-shaped token can still register it against their
# own account and repoint delivery — that needs binding the token to the
# device/install that registers it (so a repoint can be refused without
# fresh proof from that same device), which has no representation anywhere
# in this schema or the mobile client today and is a real architecture
# addition, not a validator. Left open, documented in the audit rather than
# silently narrowed to "well, the format is checked now".
EXPO_PUSH_TOKEN_PATTERN = r"^Expo(nent)?PushToken\[[A-Za-z0-9_-]+\]$"


class RegisterPushToken(Schema):
    token: str = Field(
        min_length=8, max_length=200, pattern=EXPO_PUSH_TOKEN_PATTERN
    )
    platform: str = Field(default="android", max_length=16)
