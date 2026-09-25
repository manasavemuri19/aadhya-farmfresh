"""Batch 21: AAD-PERF-003 (removing `populate_existing` after actually
testing the staleness theory it was written to guard against), plus
AAD-OPS-023 (test schema built once per session, not per test).
"""

from __future__ import annotations

from sqlalchemy import update as sa_update

from app.db.models import User as UserRow
from app.repositories.users import UserRepository

# --- AAD-PERF-003: populate_existing removed, tested rather than assumed --


async def test_get_by_id_sees_an_address_saved_through_a_prior_orm_load(session, user):
    """The exact sequence the removed comment described: an ORM load of the
    user (`update_profile`'s `session.get(UserRow, ...)`, which eagerly
    populates `.addresses` too since it's `lazy="selectin"` at the model
    level), then a Core-level `upsert_address` write that never touches
    that already-loaded object, then a plain `get_by_id` with no
    `populate_existing` at all. If this ever goes stale again (a
    SQLAlchemy upgrade, a new caching layer), this is the test that catches
    it — `populate_existing` should come back, scoped to whatever call
    actually needs it, not restored blanket.
    """
    users = UserRepository(session)
    await users.update_profile(user["id"], {"name": "Renamed"})

    await users.upsert_address(
        user["id"],
        {
            "label": "Home", "line1": "1 Farm Road", "line2": "", "landmark": "",
            "city": "Hyderabad", "pincode": "500001", "latitude": None, "longitude": None,
        },
    )

    result = await users.get_by_id(user["id"])
    assert len(result["addresses"]) == 1
    assert result["addresses"][0]["label"] == "Home"
    assert result["addresses"][0]["line1"] == "1 Farm Road"


async def test_get_by_id_sees_a_scalar_column_changed_by_a_raw_core_update(session, user):
    """The other half of the same theory, for a plain column rather than a
    relationship: load the user once (identity-maps it), change `name` via
    a raw Core `UPDATE` that bypasses the ORM object entirely (the same
    shape `erase()` uses for its scalar writes), then read it again with no
    `populate_existing`. Confirms scalar columns refresh correctly too, not
    just the `addresses` relationship the original comment specifically
    named.
    """
    users = UserRepository(session)
    await users.get_by_id(user["id"])  # identity-maps the row

    await session.execute(
        sa_update(UserRow).where(UserRow.id == user["id"]).values(name="Renamed via Core")
    )
    await session.flush()

    result = await users.get_by_id(user["id"])
    assert result["name"] == "Renamed via Core"
