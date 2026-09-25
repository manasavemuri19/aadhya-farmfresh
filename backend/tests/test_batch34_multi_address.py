"""AAD-MOB-022 (multi-address support) — the backend already stored more
than one address per user (a real `addresses` table, one row per distinct
label, capped at MAX_ADDRESSES_PER_USER), but nothing ever let a customer
*rename* one without leaving a duplicate behind, or *remove* one at all —
`upsert_address` could only create a new label or overwrite an existing
one's fields in place. This is what the product decision ("Yes want it
definitely, built it") actually needed: a real rename (`upsert_address`'s
new `previous_label` argument, and the `PATCH .../addresses/{label}` route
that uses it) and a real delete (`UserRepository.delete_address`, and
`DELETE .../addresses/{label}`).

Also covers the `Address.label` schema tightening that went with it
(`min_length=1` — a blank label was previously valid and would have
collided with every other blank-labelled row's own upsert).
"""

from __future__ import annotations

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import deps
from app.core.errors import Conflict, NotFound
from app.core.security import issue_access_token
from app.repositories.users import MAX_ADDRESSES_PER_USER, UserRepository
from app.schemas.auth import Address


def _address(label: str, line1: str = "12 Farm Road") -> dict:
    return {
        "label": label, "line1": line1, "line2": "", "landmark": "",
        "city": "Hyderabad", "pincode": "500001", "latitude": None, "longitude": None,
    }


@pytest.fixture
def users(session) -> UserRepository:
    return UserRepository(session)


async def _make_user(users, session, sub: str = "multi_addr_sub"):
    user = await users.get_or_create_by_google(
        google_sub=sub, email=f"{sub}@example.com", name="Multi Address Test",
    )
    await session.flush()
    return user


# ---------- schema: Address.label min_length=1 ----------


def test_address_rejects_a_blank_label():
    with pytest.raises(ValidationError):
        Address(**_address(""))


def test_address_accepts_a_one_character_label():
    # Not everything needs to be "Home"/"Work" — any non-empty name is fine.
    Address(**_address("A"))


# ---------- UserRepository.upsert_address: rename (previous_label) ----------


async def test_rename_relabels_the_row_in_place_not_a_second_row(users, session):
    user = await _make_user(users, session)
    await users.upsert_address(user["id"], _address("Home", line1="Old street"))

    await users.upsert_address(
        user["id"], _address("Work", line1="Old street"), previous_label="Home"
    )

    reread = await users.get_by_id(user["id"])
    labels = {a["label"] for a in reread["addresses"]}
    assert labels == {"Work"}, "must be a rename, not Home staying behind alongside Work"
    assert reread["addresses"][0]["line1"] == "Old street"


async def test_rename_also_updates_other_fields_in_the_same_call(users, session):
    user = await _make_user(users, session)
    await users.upsert_address(user["id"], _address("Home", line1="Old street"))

    await users.upsert_address(
        user["id"], _address("Work", line1="New street"), previous_label="Home"
    )

    reread = await users.get_by_id(user["id"])
    assert reread["addresses"][0]["label"] == "Work"
    assert reread["addresses"][0]["line1"] == "New street"


async def test_rename_to_the_same_label_behaves_like_a_plain_field_update(users, session):
    """previous_label == the new label isn't really a rename — the route
    layer may call this with both equal (editing fields without renaming),
    and it must take the plain upsert-by-label path, not the rename one."""
    user = await _make_user(users, session)
    await users.upsert_address(user["id"], _address("Home", line1="Old street"))

    await users.upsert_address(
        user["id"], _address("Home", line1="New street"), previous_label="Home"
    )

    reread = await users.get_by_id(user["id"])
    assert len(reread["addresses"]) == 1
    assert reread["addresses"][0]["line1"] == "New street"


async def test_rename_onto_an_already_taken_label_is_a_conflict(users, session):
    user = await _make_user(users, session)
    await users.upsert_address(user["id"], _address("Home"))
    await users.upsert_address(user["id"], _address("Work"))

    with pytest.raises(Conflict):
        await users.upsert_address(
            user["id"], _address("Work", line1="Trying to steal the label"), previous_label="Home"
        )

    # Both originals untouched.
    reread = await users.get_by_id(user["id"])
    assert {a["label"] for a in reread["addresses"]} == {"Home", "Work"}


async def test_renaming_a_label_that_does_not_exist_is_not_found(users, session):
    user = await _make_user(users, session)

    with pytest.raises(NotFound):
        await users.upsert_address(
            user["id"], _address("Work"), previous_label="DoesNotExist"
        )


async def test_rename_does_not_count_against_the_address_cap(users, session):
    """A rename never adds a row, so it must keep working even when the
    account is already sitting at MAX_ADDRESSES_PER_USER — unlike a
    genuinely new label, which the cap does correctly block."""
    user = await _make_user(users, session)
    for i in range(MAX_ADDRESSES_PER_USER):
        await users.upsert_address(user["id"], _address(f"Label{i}"))

    await users.upsert_address(
        user["id"], _address("RenamedLabel0", line1="Renamed at the cap"), previous_label="Label0"
    )

    reread = await users.get_by_id(user["id"])
    assert len(reread["addresses"]) == MAX_ADDRESSES_PER_USER
    renamed = next(a for a in reread["addresses"] if a["label"] == "RenamedLabel0")
    assert renamed["line1"] == "Renamed at the cap"


async def test_renaming_one_users_address_never_touches_another_users_same_label(users, session):
    a = await _make_user(users, session, sub="multi_addr_a")
    b = await _make_user(users, session, sub="multi_addr_b")
    await users.upsert_address(a["id"], _address("Home", line1="A's street"))
    await users.upsert_address(b["id"], _address("Home", line1="B's street"))

    with pytest.raises(NotFound):
        # a's "Office" doesn't exist — must not somehow match b's "Home".
        await users.upsert_address(a["id"], _address("NewLabel"), previous_label="Office")

    reread_b = await users.get_by_id(b["id"])
    assert reread_b["addresses"][0]["line1"] == "B's street", "b's row must be untouched"


# ---------- UserRepository.delete_address ----------


async def test_delete_address_removes_the_row(users, session):
    user = await _make_user(users, session)
    await users.upsert_address(user["id"], _address("Home"))
    await users.upsert_address(user["id"], _address("Work"))

    deleted = await users.delete_address(user["id"], "Home")

    assert deleted is True
    reread = await users.get_by_id(user["id"])
    assert {a["label"] for a in reread["addresses"]} == {"Work"}


async def test_delete_address_for_an_unknown_label_reports_false(users, session):
    user = await _make_user(users, session)
    await users.upsert_address(user["id"], _address("Home"))

    deleted = await users.delete_address(user["id"], "DoesNotExist")

    assert deleted is False
    reread = await users.get_by_id(user["id"])
    assert len(reread["addresses"]) == 1, "the real address must be untouched"


async def test_delete_address_never_touches_another_users_same_label(users, session):
    a = await _make_user(users, session, sub="multi_addr_del_a")
    b = await _make_user(users, session, sub="multi_addr_del_b")
    await users.upsert_address(a["id"], _address("Home", line1="A's street"))
    await users.upsert_address(b["id"], _address("Home", line1="B's street"))

    deleted = await users.delete_address(a["id"], "Home")

    assert deleted is True
    reread_a = await users.get_by_id(a["id"])
    reread_b = await users.get_by_id(b["id"])
    assert reread_a["addresses"] == []
    assert reread_b["addresses"][0]["line1"] == "B's street", "b's row must survive a's delete"


# ---------- HTTP layer: PATCH / DELETE .../me/addresses/{label} ----------


@pytest.fixture
async def client(engine):
    """Same shape as test_http_layer.py's own `client` fixture — a real
    ASGI client against the real app, a `db_session` override bound to this
    test's engine. Redefined here (rather than imported) since pytest
    fixtures aren't shared across files without a conftest.py entry, same
    reasoning test_support_admin.py's own docstring gives for its local
    `support` fixture.
    """
    from app.main import app as real_app

    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async def override_db_session(request: Request):
        session = factory()
        request.state.db_session = session
        yield session

    real_app.dependency_overrides[deps.db_session] = override_db_session
    transport = ASGITransport(app=real_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    real_app.dependency_overrides.pop(deps.db_session, None)


@pytest.fixture
async def http_user(engine):
    """A real user, committed for real on the same engine `client` talks
    to — each HTTP request opens its own fresh session, same as two
    separate requests would in production."""
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        user = await UserRepository(session).get_or_create_by_google(
            google_sub="multi_addr_http_sub", email="http@example.com", name="HTTP Test",
        )
        await session.commit()
    return user


def _auth(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {issue_access_token(user_id, role='customer')}"}


async def test_patch_addresses_renames_over_http(client, http_user):
    await client.put(
        "/v1/auth/me/addresses", json=_address("Home"), headers=_auth(http_user["id"])
    )

    resp = await client.patch(
        "/v1/auth/me/addresses/Home", json=_address("Work"), headers=_auth(http_user["id"])
    )

    assert resp.status_code == 204
    me = await client.get("/v1/auth/me", headers=_auth(http_user["id"]))
    labels = {a["label"] for a in me.json()["addresses"]}
    assert labels == {"Work"}


async def test_patch_addresses_for_an_unknown_label_is_404(client, http_user):
    resp = await client.patch(
        "/v1/auth/me/addresses/DoesNotExist",
        json=_address("Work"),
        headers=_auth(http_user["id"]),
    )
    assert resp.status_code == 404


async def test_patch_addresses_onto_a_taken_label_is_409(client, http_user):
    await client.put("/v1/auth/me/addresses", json=_address("Home"), headers=_auth(http_user["id"]))
    await client.put("/v1/auth/me/addresses", json=_address("Work"), headers=_auth(http_user["id"]))

    resp = await client.patch(
        "/v1/auth/me/addresses/Home", json=_address("Work"), headers=_auth(http_user["id"])
    )

    assert resp.status_code == 409


async def test_delete_addresses_removes_over_http(client, http_user):
    await client.put("/v1/auth/me/addresses", json=_address("Home"), headers=_auth(http_user["id"]))

    resp = await client.delete("/v1/auth/me/addresses/Home", headers=_auth(http_user["id"]))

    assert resp.status_code == 204
    me = await client.get("/v1/auth/me", headers=_auth(http_user["id"]))
    assert me.json()["addresses"] == []


async def test_delete_addresses_for_an_unknown_label_is_404(client, http_user):
    resp = await client.delete("/v1/auth/me/addresses/DoesNotExist", headers=_auth(http_user["id"]))
    assert resp.status_code == 404
