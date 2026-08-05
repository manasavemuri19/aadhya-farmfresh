from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Schema(BaseModel):
    model_config = ConfigDict(
        extra="forbid",            # reject unknown fields instead of silently dropping them
        str_strip_whitespace=True,
        populate_by_name=True,
    )


class Page(Schema, Generic[T]):
    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


class Money(Schema):
    """Wire format for money. `paise` is authoritative; the rest is convenience."""

    paise: int = Field(ge=0)
    currency: str = "INR"

    @property
    def rupees_display(self) -> str:
        whole, frac = divmod(self.paise, 100)
        return f"{whole:,}.{frac:02d}"


class Timestamped(Schema):
    created_at: datetime
    updated_at: datetime
