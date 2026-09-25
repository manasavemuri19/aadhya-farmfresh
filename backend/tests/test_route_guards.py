"""AAD-OPS-021 — the second half of its suggested fix: "add a test that
enumerates the app's routes and asserts each one's dependency set matches an
expected table — that single test prevents the whole class of 'wrong guard'
bug permanently." This is that test.

It walks each route's FastAPI `Dependant` tree (recursively, since a guard
like `require_staff` itself depends on `current_user` — the strongest guard
present wins) and classifies every route as `public`, `customer`
(`CurrentUser`), `staff` (`StaffUser`), `admin` (`AdminUser`) or
`delivery_agent` (`DeliveryAgentUser`). That classification is compared
against an explicit table below, one row per route.

This is exactly the mechanism `AAD-SEC-025` needed and didn't have: nothing
previously asserted which gate protects which route, so a route wired to
`StaffUser` instead of the `AdminUser` it should have used was invisible.
A future route that's added with no guard at all, or the wrong one, now
fails this test immediately rather than shipping unnoticed.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from app.api.deps import current_user, require_admin, require_delivery_agent, require_staff
from app.api.v1.routes import (
    admin,
    auth,
    catalog,
    delivery,
    health,
    notifications,
    orders,
    payments,
    support,
)

# (module, method, path) -> expected guard.
# "public" means no authentication dependency at all — deliberately, for
# every row here (an anonymous cart quote, the catalog, auth's own entry
# points, the payment webhook/redirect/callback which are verified by
# signature or gateway-issued token rather than a bearer credential, and the
# mock payment helper routes used only against the mock provider in tests).
EXPECTED_GUARDS: dict[tuple[str, str, str], str] = {
    ("admin", "GET", "/admin/products"): "staff",
    ("admin", "POST", "/admin/stock"): "staff",
    ("admin", "POST", "/admin/price"): "admin",
    ("admin", "POST", "/admin/products/{sku}/availability"): "staff",
    ("admin", "GET", "/admin/orders"): "staff",
    ("admin", "POST", "/admin/orders/{order_id}/status"): "staff",
    ("admin", "POST", "/admin/orders/{order_id}/reassign"): "staff",
    ("admin", "POST", "/admin/maintenance/release-holds"): "staff",
    # AAD-BIZ-004: real-cash reconciliation gets the same owner-only bar a
    # refund does (see update_order_status's own REFUNDED check above it in
    # admin.py) — not a staff action.
    ("admin", "POST", "/admin/cod/settlements"): "admin",
    ("admin", "GET", "/admin/support/tickets"): "staff",
    ("admin", "POST", "/admin/support/tickets/{ticket_id}/close"): "staff",
    ("auth", "POST", "/auth/google"): "public",
    ("auth", "POST", "/auth/refresh"): "public",
    ("auth", "POST", "/auth/logout"): "public",
    ("auth", "POST", "/auth/logout-all"): "customer",
    ("auth", "GET", "/auth/me"): "customer",
    ("auth", "PATCH", "/auth/me"): "customer",
    ("auth", "DELETE", "/auth/me"): "customer",
    ("auth", "PUT", "/auth/me/addresses"): "customer",
    ("auth", "PATCH", "/auth/me/addresses/{label}"): "customer",
    ("auth", "DELETE", "/auth/me/addresses/{label}"): "customer",
    ("catalog", "GET", "/catalog"): "public",
    ("catalog", "GET", "/catalog/products/{id_or_slug}"): "public",
    ("catalog", "GET", "/catalog/search"): "public",
    ("delivery", "GET", "/delivery/requests"): "delivery_agent",
    ("delivery", "GET", "/delivery/ongoing"): "delivery_agent",
    ("delivery", "POST", "/delivery/orders/{order_id}/accept"): "delivery_agent",
    ("delivery", "POST", "/delivery/orders/{order_id}/release"): "delivery_agent",
    ("delivery", "POST", "/delivery/orders/{order_id}/status"): "delivery_agent",
    ("delivery", "POST", "/delivery/orders/{order_id}/verify-delivery"): "delivery_agent",
    ("delivery", "POST", "/delivery/location"): "delivery_agent",
    ("health", "GET", "/health/live"): "public",
    ("health", "GET", "/health"): "public",
    ("notifications", "POST", "/notifications/register-token"): "customer",
    ("notifications", "DELETE", "/notifications/token"): "customer",
    ("orders", "POST", "/cart/quote"): "public",
    ("orders", "POST", "/orders"): "customer",
    ("orders", "GET", "/orders"): "customer",
    ("orders", "GET", "/orders/{order_id}"): "customer",
    ("orders", "POST", "/orders/{order_id}/cancel"): "customer",
    ("orders", "PATCH", "/orders/{order_id}/address"): "customer",
    ("orders", "POST", "/orders/{order_id}/retry-payment"): "customer",
    ("payments", "POST", "/payments/webhook"): "public",
    ("payments", "GET", "/payments/mock/sign"): "public",
    ("payments", "POST", "/payments/mock/complete"): "customer",
    ("payments", "GET", "/payments/link-redirect"): "public",
    ("payments", "GET", "/payments/link-callback"): "public",
    ("support", "POST", "/support/tickets"): "customer",
}

_MODULES = {
    "admin": admin,
    "auth": auth,
    "catalog": catalog,
    "delivery": delivery,
    "health": health,
    "notifications": notifications,
    "orders": orders,
    "payments": payments,
    "support": support,
}


def _collect_dependency_calls(dependant, seen: set[int] | None = None) -> set:
    """Every callable reachable from this route's dependency tree,
    recursively — a guard like `require_staff` shows up alongside
    `current_user`, since it depends on it internally."""
    if seen is None:
        seen = set()
    calls: set = set()
    if dependant is None:
        return calls
    call = getattr(dependant, "call", None)
    if call is not None:
        calls.add(call)
    for sub in getattr(dependant, "dependencies", None) or []:
        if id(sub) in seen:
            continue
        seen.add(id(sub))
        calls |= _collect_dependency_calls(sub, seen)
    return calls


def _classify_guard(route: APIRoute) -> str:
    calls = _collect_dependency_calls(route.dependant)
    if require_admin in calls:
        return "admin"
    if require_staff in calls:
        return "staff"
    if require_delivery_agent in calls:
        return "delivery_agent"
    if current_user in calls:
        return "customer"
    return "public"


def _actual_routes() -> dict[tuple[str, str, str], str]:
    actual: dict[tuple[str, str, str], str] = {}
    for name, mod in _MODULES.items():
        for route in mod.router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in sorted(route.methods or ()):
                if method == "HEAD":
                    continue  # FastAPI adds this alongside GET automatically
                actual[(name, method, route.path)] = _classify_guard(route)
    return actual


def test_every_route_is_guarded_by_exactly_the_expected_dependency():
    actual = _actual_routes()

    missing_from_table = set(actual) - set(EXPECTED_GUARDS)
    assert not missing_from_table, (
        f"New or renamed route(s) not in EXPECTED_GUARDS — add them with the "
        f"intended guard: {sorted(missing_from_table)}"
    )

    removed_routes = set(EXPECTED_GUARDS) - set(actual)
    assert not removed_routes, (
        f"EXPECTED_GUARDS names route(s) that no longer exist — remove them: "
        f"{sorted(removed_routes)}"
    )

    mismatches = {
        key: (EXPECTED_GUARDS[key], actual[key])
        for key in EXPECTED_GUARDS
        if EXPECTED_GUARDS[key] != actual[key]
    }
    assert not mismatches, (
        "Route(s) guarded by something other than expected "
        "(module, method, path) -> (expected, actual): "
        f"{mismatches}"
    )


def test_no_admin_only_route_is_reachable_by_plain_staff():
    """AAD-SEC-025's own regression guard, expressed structurally: at least
    one route must still require the strictly-stronger `admin` gate, so a
    future refactor that quietly downgrades every admin action to `staff`
    doesn't pass silently."""
    actual = _actual_routes()
    admin_routes = [key for key, guard in actual.items() if guard == "admin"]
    assert admin_routes, "No route requires AdminUser — set_price should"
