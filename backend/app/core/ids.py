"""Identifier generation.

Public ids are opaque, URL-safe and prefixed, so a value that leaks into a log
or a support ticket is immediately identifiable. We deliberately avoid exposing
raw database primary keys, which can encode a creation order and be
enumerable-adjacent.

AAD-QUAL-004: this docstring used to say "Mongo ObjectIds" — a leftover from
before this app was ported to PostgreSQL/SQLAlchemy. This module has never
generated or depended on a Mongo ObjectId.
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


def new_support_ticket_id() -> str:
    return new_id("sup")


def new_refresh_token_id() -> str:
    return new_id("rtok")
