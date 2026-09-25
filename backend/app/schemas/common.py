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


def _indian_grouped(n: int) -> str:
    """Format a non-negative integer with Indian (lakh/crore) digit
    grouping: the last three digits are one group, then every pair of
    digits before that gets its own comma. 1234567 -> "12,34,567"."""
    s = str(n)
    if len(s) <= 3:
        return s
    last_three, rest = s[-3:], s[:-3]
    groups = []
    while len(rest) > 2:
        groups.append(rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.append(rest)
    return ",".join(reversed(groups)) + "," + last_three


class Money(Schema):
    """Wire format for money. `paise` is authoritative; the rest is convenience."""

    paise: int = Field(ge=0)
    currency: str = "INR"

    @property
    def rupees_display(self) -> str:
        """AAD-QUAL-008: `f"{whole:,}"` groups by thousands (12,34,567 would
        render as 1,234,567) — correct for USD, wrong for every customer
        above INR 1 lakh in this app. Indian digit grouping puts a comma
        every two digits after the first three from the right: the last
        three digits form one group, then every pair of digits before that
        gets its own comma (1,23,456 · 12,34,567 · 1,23,45,678)."""
        whole, frac = divmod(self.paise, 100)
        return f"{_indian_grouped(whole)}.{frac:02d}"


class Timestamped(Schema):
    created_at: datetime
    updated_at: datetime
