"""Server-side refresh-token store.

The persistence half of AAD-SEC-002's revocation and rotation design — see
`db/models.py:RefreshToken` for the column-level rationale (why a hash and
not the token, why `replaced_by` is the reuse signal). `AuthService.refresh`
is where these methods get composed into the actual rotate-or-revoke-family
decision; this repository only does the CRUD.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import RefreshToken


def hash_token(raw_token: str) -> str:
    """SHA-256 of the raw JWT. Stored instead of the token itself, so a
    leaked row — a DB dump, a stray log line — is not, on its own, a usable
    session; the presented token must still hash to this value."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _to_dict(row: RefreshToken) -> dict[str, Any]:
    return {
        "jti": row.jti,
        "user_id": row.user_id,
        "token_hash": row.token_hash,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
        "replaced_by": row.replaced_by,
    }


class RefreshTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        jti: str,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
        device_label: str | None = None,
    ) -> None:
        self.session.add(
            RefreshToken(
                jti=jti,
                user_id=user_id,
                token_hash=token_hash,
                expires_at=expires_at,
                device_label=device_label,
            )
        )
        await self.session.flush()

    async def get(self, jti: str) -> dict[str, Any] | None:
        row = (
            await self.session.execute(select(RefreshToken).where(RefreshToken.jti == jti))
        ).scalars().first()
        return _to_dict(row) if row else None

    async def mark_rotated(self, jti: str, *, replaced_by: str) -> bool:
        """The atomic half of rotation: revoke this row and point it at its
        successor, but only if it was still live. A CAS, not a blind
        update — `revoked_at IS NULL` in the WHERE clause means two
        concurrent refreshes with the same token can't both "win"; the
        loser's False is exactly the reuse signal the caller needs to act
        on, indistinguishable at this layer from an actual stolen-token
        replay and handled the same way by design.
        """
        result = await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.jti == jti, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC), replaced_by=replaced_by)
        )
        return (result.rowcount or 0) > 0

    async def revoke(self, jti: str) -> None:
        """Logout — ends a session on purpose. `replaced_by` stays NULL,
        which is what tells `refresh` this was an intentional sign-out, not
        theft, if the token is ever presented again."""
        await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.jti == jti, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    async def revoke_all_for_user(self, user_id: str) -> None:
        """Logout-all, and the reuse-detected family revocation in
        `AuthService.refresh` — both need every live session for this user
        to stop working immediately, not just the one token in hand."""
        await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    async def delete_expired(self, *, batch_size: int = 500) -> int:
        """Once a row's `expires_at` has passed, the JWT it corresponds to
        already fails its own `exp` check before ever reaching this table —
        the row has no further security purpose (not even for reuse
        detection) and only exists to be pruned, same reasoning as the OTP
        and idempotency sweeps this runs alongside.

        AAD-REL-003: batched the same way as `IdempotencyRepository.
        delete_expired` — a LIMIT-bounded id (`jti`) subquery, looped until
        drained, so a large backlog can't take one long lock across the
        whole table. `ix_refresh_tokens_expires` keeps each batch's subquery
        cheap.
        """
        total = 0
        while True:
            batch = select(RefreshToken.jti).where(
                RefreshToken.expires_at < datetime.now(UTC)
            ).limit(batch_size)
            result = await self.session.execute(
                delete(RefreshToken).where(RefreshToken.jti.in_(batch))
            )
            deleted = result.rowcount or 0
            total += deleted
            if deleted < batch_size:
                return total
