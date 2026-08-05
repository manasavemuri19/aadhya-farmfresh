"""A single error vocabulary shared by every layer.

Services raise `AppError` subclasses; the API layer turns them into a stable
JSON envelope. Clients switch on `error.code`, never on the message string.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status_code: int = 400
    code: str = "bad_request"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return {"error": body}


class ValidationError(AppError):
    status_code, code = 422, "validation_error"


class Unauthorized(AppError):
    status_code, code = 401, "unauthorized"


class Forbidden(AppError):
    status_code, code = 403, "forbidden"


class NotFound(AppError):
    status_code, code = 404, "not_found"


class Conflict(AppError):
    status_code, code = 409, "conflict"


class RateLimited(AppError):
    status_code, code = 429, "rate_limited"


class OutOfStock(Conflict):
    code = "out_of_stock"


class PriceChanged(Conflict):
    code = "price_changed"


class InvalidStateTransition(Conflict):
    code = "invalid_state_transition"


class PaymentFailed(AppError):
    status_code, code = 402, "payment_failed"


class UpstreamError(AppError):
    status_code, code = 502, "upstream_error"
