"""Money handling.

Rule for this codebase: money is an `int` of paise, everywhere — database,
API payloads, business logic. Floats never touch a price. The client formats
for display; the server is the only thing that computes.
"""

from __future__ import annotations

MAX_PAISE = 100_000_000  # 10 lakh rupees — a sanity ceiling on any single amount


def rupees_to_paise(rupees: int | str) -> int:
    """Convert a whole-rupee value to paise. Accepts str to avoid float input."""
    return int(str(rupees).strip()) * 100


def format_inr(paise: int) -> str:
    """Render paise as a rupee string. Server-side use only (logs, receipts)."""
    sign = "-" if paise < 0 else ""
    whole, fraction = divmod(abs(paise), 100)
    return f"{sign}Rs {whole:,}.{fraction:02d}"


def assert_valid_amount(paise: int, *, field: str = "amount") -> int:
    if not isinstance(paise, int) or isinstance(paise, bool):
        raise TypeError(f"{field} must be an int of paise, got {type(paise).__name__}")
    if paise < 0:
        raise ValueError(f"{field} must not be negative")
    if paise > MAX_PAISE:
        raise ValueError(f"{field} exceeds the maximum allowed amount")
    return paise
