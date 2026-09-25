"""A single error vocabulary shared by every layer.

Services raise `AppError` subclasses; the API layer turns them into a stable
JSON envelope. Clients switch on `error.code`, never on the message string.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import request_id_var


class AppError(Exception):
    status_code: int = 400
    code: str = "bad_request"

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        # AAD-SEC-004: RateLimited is the one error that needs to hand the
        # client something beyond the JSON body — Retry-After. Generic on
        # AppError rather than special-cased in the exception handler, so
        # any future error that needs a response header can use the same
        # mechanism without another special case.
        self.headers = headers or {}

    def to_payload(self) -> dict[str, Any]:
        # AAD-QUAL-010: previously only the catch-all 500 handler in main.py
        # attached a request_id — every AppError subclass (a 404, a 409, a
        # 422 validation failure) returned an envelope without one, so a
        # customer could quote a request id from a crash but not from an
        # ordinary rejected request, which is the one they're actually more
        # likely to be looking at and asking support about. Read from the
        # same ContextVar the JSON log formatter uses, so it's always
        # whatever RequestContextMiddleware set for this request — "-"
        # outside a request context (a unit test constructing an error
        # directly), same as an unformatted log line would show.
        body: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "request_id": request_id_var.get(),
        }
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


class CodUnavailableError(ValidationError):
    """AAD-BIZ-002: cash on delivery refused for this cart or this account,
    but the same cart can still be placed by paying online — a 422, not a
    403: nothing about who the customer is is being refused, just this one
    payment method for this one order right now.

    Named with the `Error` suffix `ruff`'s `N818` expects, unlike its older
    siblings above (`Forbidden`, `NotFound`, `Conflict`, ...) — those
    predate this rule being enforced here and are pre-existing, disclosed
    debt, not something to fix as a drive-by on an unrelated finding."""

    code = "cod_unavailable"
