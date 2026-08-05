"""Identifier generation.

Public ids are opaque, URL-safe and prefixed, so a value that leaks into a log
or a support ticket is immediately identifiable. We deliberately avoid exposing
Mongo ObjectIds, which encode a creation timestamp and are enumerable-adjacent.
"""

from __future__ import annotations

import secrets
import string

_ALPHABET = string.ascii_lowercase + string.digits


def _token(length: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def new_id(prefix: str, length: int = 20) -> str:
    return f"{prefix}_{_token(length)}"


def new_user_id() -> str:
    return new_id("usr")


def new_order_id() -> str:
    return new_id("ord")


def new_payment_id() -> str:
    return new_id("pay")


def human_order_number() -> str:
    """Short, readable reference the delivery rider and customer can say aloud."""
    return "AD" + "".join(secrets.choice(string.digits) for _ in range(6))


def new_otp_code(digits: int = 6) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(digits))
