"""AAD-BIZ-003 — the owner had no way to learn a SKU had run low until they
happened to check the app themselves. This adds a debounced low-stock push
to every staff/admin account, reusing the `low_stock_threshold` column that
already drives the in-app "low stock" badge (`app/schemas/catalog.py`) —
the owner hasn't given us a value to change any SKU's threshold to yet, so
this notifies off whatever each variant is already set to; the threshold
itself is out of scope here.

Covers three layers: `ProductRepository`'s three new methods directly
(`find_newly_low_stock`, `mark_low_stock_notified`,
`clear_stale_low_stock_flags`), then the housekeeping sweep's own wiring
end to end (`app.main._notify_low_stock` / `_run_sweep_once`) — including
the debounce (`low_stock_notified`) and the re-arm once stock next leaves
the band, restocked or sold through to zero.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from app.db.models import Variant as VariantRow
from app.domain.enums import Role
from app.repositories.users import UserRepository


async def _set_variant(session, sku: str, **values) -> None:
    await session.execute(update(VariantRow).where(VariantRow.sku == sku).values(**values))
    await session.flush()


async def _low_stock_notified(session, sku: str) -> bool:
    """A bare column select, not `select(VariantRow)` — matching the
    pattern the rest of this suite already uses (e.g.
    test_stock_admin_integrity.py's own `select(VariantRow.stock_qty)`).
    With `expire_on_commit=False` (this session fixture's own setting), a
    full-entity select would hand back the *same*, already-loaded ORM
    object from the identity map — e.g. the one the `milk` fixture first
    populated — without refreshing its attributes from a row committed
    by a completely different connection (`_run_sweep_once`'s own
    `session_scope()`). A column-only select has no identity-mapped
    object to reuse, so it always reflects the latest committed value.
    """
    return (
        await session.execute(
            select(VariantRow.low_stock_notified).where(VariantRow.sku == sku)
        )
    ).scalar_one()


# ---------- ProductRepository: find_newly_low_stock ----------


async def test_find_newly_low_stock_returns_a_sku_in_its_own_band(session, products, milk):
    # MILK-COW-1L seeds at stock_qty=5 with the model default threshold (3).
    await _set_variant(session, "MILK-COW-1L", stock_qty=2, low_stock_threshold=3)

    found = await products.find_newly_low_stock()

    assert [row["sku"] for row in found] == ["MILK-COW-1L"]
    assert found[0]["stock_qty"] == 2


async def test_find_newly_low_stock_excludes_a_sold_out_sku(session, products, milk):
    """Zero stock already reads as "sold out" in-app — nothing left to
    restock-soon about, so it's excluded rather than treated as the most
    urgent low-stock case."""
    await _set_variant(session, "MILK-COW-1L", stock_qty=0, low_stock_threshold=3)

    found = await products.find_newly_low_stock()

    assert found == []


async def test_find_newly_low_stock_excludes_a_sku_above_its_threshold(session, products, milk):
    # MILK-COW-1L: stock_qty=5 > low_stock_threshold=3 by default — not low.
    found = await products.find_newly_low_stock()
    assert found == []


async def test_find_newly_low_stock_excludes_an_already_notified_sku(session, products, milk):
    await _set_variant(
        session, "MILK-COW-1L", stock_qty=2, low_stock_threshold=3, low_stock_notified=True
    )

    found = await products.find_newly_low_stock()

    assert found == [], "already flagged — the sweep's debounce, not this query, re-arms it"


async def test_find_newly_low_stock_excludes_an_inactive_sku(session, products, milk):
    """A delisted/disabled variant isn't something an owner needs woken up
    for — it's not sellable regardless of what's left in the stockroom."""
    await _set_variant(session, "MILK-COW-1L", stock_qty=2, low_stock_threshold=3, is_active=False)

    found = await products.find_newly_low_stock()

    assert found == []


# ---------- ProductRepository: mark_low_stock_notified / clear_stale_low_stock_flags ----------


async def test_mark_low_stock_notified_sets_the_flag(session, products, milk):
    await products.mark_low_stock_notified(["MILK-COW-1L"])

    notified = await _low_stock_notified(session, "MILK-COW-1L")
    assert notified is True


async def test_mark_low_stock_notified_with_no_skus_is_a_no_op(session, products, milk):
    # Must not raise on an empty IN () — see the method's own early return.
    await products.mark_low_stock_notified([])


async def test_clear_stale_low_stock_flags_rearms_once_restocked(session, products, milk):
    await _set_variant(
        session, "MILK-COW-1L", stock_qty=2, low_stock_threshold=3, low_stock_notified=True
    )
    await _set_variant(session, "MILK-COW-1L", stock_qty=50)  # restocked, still flagged

    cleared = await products.clear_stale_low_stock_flags()

    assert cleared == 1
    notified = await _low_stock_notified(session, "MILK-COW-1L")
    assert notified is False


async def test_clear_stale_low_stock_flags_rearms_once_sold_out(session, products, milk):
    await _set_variant(
        session, "MILK-COW-1L", stock_qty=1, low_stock_threshold=3, low_stock_notified=True
    )
    await _set_variant(session, "MILK-COW-1L", stock_qty=0)  # sold through, still flagged

    cleared = await products.clear_stale_low_stock_flags()

    assert cleared == 1
    notified = await _low_stock_notified(session, "MILK-COW-1L")
    assert notified is False


async def test_clear_stale_low_stock_flags_leaves_a_still_low_sku_alone(session, products, milk):
    await _set_variant(
        session, "MILK-COW-1L", stock_qty=2, low_stock_threshold=3, low_stock_notified=True
    )

    cleared = await products.clear_stale_low_stock_flags()

    assert cleared == 0
    notified = await _low_stock_notified(session, "MILK-COW-1L")
    assert notified is True, "still in the band — must stay flagged, not re-notify"


# ---------- end to end: the housekeeping sweep ----------


class _FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"data": []}


class _FakeAsyncClient:
    """Same shape as test_push_service.py's own fake — records every batch
    of Expo push messages `PushService._send` would have posted, without
    making a real HTTP call."""

    def __init__(self, *_a, **_kw):
        self.calls: list[list[dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, *, json, headers):
        self.calls.append(json)
        return _FakeResponse()


@pytest.fixture
def fake_push_client(monkeypatch):
    import app.services.push_service as push_service_module

    fake = _FakeAsyncClient()
    monkeypatch.setattr(push_service_module.httpx, "AsyncClient", lambda **kw: fake)
    return fake


@pytest.fixture
async def sweep_db():
    """`_run_sweep_once` opens its own connection via `app.db.base`'s module-
    level engine (`session_scope()`), a separate connection from the test's
    own `session` fixture — same reasoning and same pattern as
    test_ops_hardening.py's sweep-lock test: connect for the duration of
    the test, disconnect after, so this doesn't leak a live engine into
    whatever test runs next.
    """
    from app.db import base as db

    await db.connect()
    try:
        yield
    finally:
        await db.disconnect()


async def _make_staff(session, sub: str) -> str:
    from app.db.models import User as UserRow

    staff = await UserRepository(session).get_or_create_by_google(
        google_sub=sub, email=f"{sub}@example.com", name="Staff"
    )
    await session.execute(
        update(UserRow).where(UserRow.id == staff["id"]).values(role=Role.STAFF.value)
    )
    await session.flush()
    return staff["id"]


async def test_sweep_notifies_staff_once_then_debounces(session, milk, fake_push_client, sweep_db):
    from app import main as main_module
    from app.repositories.push_tokens import PushTokenRepository

    staff_id = await _make_staff(session, "biz003_staff_1")
    await PushTokenRepository(session).register(
        user_id=staff_id, token="tok_biz003_1", platform="android"
    )
    await _set_variant(session, "MILK-COW-1L", stock_qty=2, low_stock_threshold=3)
    await session.commit()

    await main_module._run_sweep_once()

    assert len(fake_push_client.calls) == 1, "one push batch for the one low SKU"
    [messages] = fake_push_client.calls
    assert len(messages) == 1
    assert messages[0]["to"] == "tok_biz003_1"
    assert "MILK-COW-1L" in messages[0]["body"] or "1 litre" in messages[0]["body"]
    assert "2" in messages[0]["body"]

    notified = await _low_stock_notified(session, "MILK-COW-1L")
    assert notified is True

    # A second sweep pass, still at the same low stock level, must not
    # push again — that's the whole point of the debounce.
    await main_module._run_sweep_once()
    assert len(fake_push_client.calls) == 1, "debounced: no second push while still low"


async def test_sweep_notifies_only_once_more_after_a_restock_and_a_second_dip(
    session, milk, fake_push_client, sweep_db
):
    from app import main as main_module
    from app.repositories.push_tokens import PushTokenRepository

    staff_id = await _make_staff(session, "biz003_staff_2")
    await PushTokenRepository(session).register(
        user_id=staff_id, token="tok_biz003_2", platform="android"
    )
    await _set_variant(session, "MILK-COW-1L", stock_qty=2, low_stock_threshold=3)
    await session.commit()

    await main_module._run_sweep_once()
    assert len(fake_push_client.calls) == 1

    # Restocked well above the threshold — the next sweep should re-arm
    # the flag (clear_stale_low_stock_flags), with no push of its own.
    await _set_variant(session, "MILK-COW-1L", stock_qty=50)
    await session.commit()
    await main_module._run_sweep_once()
    assert len(fake_push_client.calls) == 1, "a restock itself never pushes"
    notified = await _low_stock_notified(session, "MILK-COW-1L")
    assert notified is False

    # Dips low again — re-armed, so this is a fresh notification.
    await _set_variant(session, "MILK-COW-1L", stock_qty=1)
    await session.commit()
    await main_module._run_sweep_once()
    assert len(fake_push_client.calls) == 2, "re-armed after the restock: the dip pushes again"


async def test_sweep_with_no_low_stock_skus_pushes_nothing(
    session, milk, fake_push_client, sweep_db
):
    from app import main as main_module

    # MILK-COW-1L defaults to stock_qty=5 > threshold=3 — nothing low.
    await session.commit()

    await main_module._run_sweep_once()

    assert fake_push_client.calls == []


async def test_sweep_with_no_staff_accounts_still_marks_notified(
    session, milk, fake_push_client, sweep_db
):
    """No staff/admin exist to push to — must not raise, and the SKU is
    still marked notified so it doesn't queue up a burst of pushes the
    moment a staff account is finally created."""
    from app import main as main_module

    await _set_variant(session, "MILK-COW-1L", stock_qty=2, low_stock_threshold=3)
    await session.commit()

    await main_module._run_sweep_once()

    assert fake_push_client.calls == []
    notified = await _low_stock_notified(session, "MILK-COW-1L")
    assert notified is True
