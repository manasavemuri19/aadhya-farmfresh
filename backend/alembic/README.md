# Migration notes

## Adding an index without blocking writes (AAD-DATA-012)

`op.create_index(...)` runs a plain `CREATE INDEX`, which takes a `SHARE`
lock on the table for the whole build. On an empty or small table that's
instant and harmless. On a table with real rows in it, it blocks every
write against that table until the index finishes building — on `orders`
or `payments` once this app has real traffic, that's a multi-minute outage
on order creation, not a theoretical risk.

Every index in this migration history as of `0019_stock_ledger_order_fk`
was added, or the table it's on was created, while this app was pre-launch
with no production data — see `AAD-DATA-014` and `AAD-SEC-034` in
`PRR-AUDIT.md` for the same "pre-launch, no orphan/lock concern" reasoning
applied elsewhere. That's why none of them were retrofitted with the
pattern below: doing so would add real complexity (autocommit semantics,
a build that can fail partway and needs manual cleanup) for a table that
has never had a concurrent writer to block. **The next migration that adds
an index to a table already carrying production data is a different
situation** — that one needs this:

```python
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_whatever",
            "orders",
            ["some_column"],
            postgresql_concurrently=True,
        )
```

Two things `postgresql_concurrently=True` requires, both easy to get wrong:

- **`autocommit_block()` is not optional.** `CREATE INDEX CONCURRENTLY`
  cannot run inside a transaction at all — Postgres rejects it outright —
  and Alembic wraps every migration in one by default. `autocommit_block()`
  commits the migration's transaction, runs this one statement outside of
  any transaction, then opens a fresh one for whatever comes after it in
  the same `upgrade()`.
- **A failed concurrent build leaves an `INVALID` index behind**, because
  there's no transaction for Postgres to roll back through. It does not
  block anything (an invalid index is simply never used), but it does sit
  there taking up space and needs `DROP INDEX CONCURRENTLY ix_whatever`
  before retrying — that cleanup step won't happen on its own.

The same applies to dropping or rebuilding an index on a populated table
(`op.drop_index` also takes a lock, briefly) — use
`postgresql_concurrently=True` there too, and, if the table is one this
codebase writes to constantly (`orders`, `payments`, `variants`), prefer
adding the new index and switching readers over before dropping the old
one, rather than a drop-then-recreate in the same migration.
