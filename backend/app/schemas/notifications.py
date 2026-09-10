from __future__ import annotations

from pydantic import Field

from app.schemas.common import Schema


class RegisterPushToken(Schema):
    token: str = Field(min_length=8, max_length=200)
    platform: str = Field(default="android", max_length=16)
