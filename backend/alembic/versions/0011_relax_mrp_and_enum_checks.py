"""relax the MRP check, add CHECK constraints on enum-like columns

AAD-DATA-009: `ck_variant_mrp_above_price` used strict `mrp_paise >
price_paise`, so a variant was only valid while actively discounted —
removing a discount or raising a price to match MRP was a database-level
violation. Relaxed to `>=`, which still rejects the nonsense case (an MRP
below the selling price) while permitting list-price selling.

AAD-DATA-010: `orders.status`, `order_events.status`, `users.role`,
`payments.status`, `payments.method` and `variants.stock_policy` were all
plain unconstrained text columns, even though the application treats every
one of them as a closed set (see app/domain/enums.py). A bad value —
a manual psql fix during an incident, a future write path that bypasses the
enum — used to be silently storable and then raise an unhandled ValueError
the next time that row was read, taking down whichever endpoint tried
(`OrderService.list_for_user` maps every one of a customer's orders through
this, so one bad status row broke that customer's entire order history).
CHECK constraints make the invalid state unrepresentable rather than merely
unlikely; no data migration is needed since every value ever written was
already inside the allowed set.

Revision ID: 0011_relax_mrp_and_enum_checks
Revises: 0010_refresh_tokens
Create Date: 2026-09-14 00:00:00.000000
"""
from __future__ import annotations

from alembic import op

revision = '0011_relax_mrp_and_enum_checks'
down_revision = '0010_refresh_tokens'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # AAD-DATA-009
    op.drop_constraint('ck_variant_mrp_above_price', 'variants', type_='check')
    op.create_check_constraint(
        'ck_variant_mrp_above_price',
        'variants',
        'mrp_paise IS NULL OR mrp_paise >= price_paise',
    )

    # AAD-DATA-010
    op.create_check_constraint(
        'ck_user_role_valid',
        'users',
        "role IN ('customer', 'staff', 'admin', 'delivery_agent')",
    )
    op.create_check_constraint(
        'ck_variant_stock_policy_valid',
        'variants',
        "stock_policy IN ('tracked', 'made_to_order')",
    )
    op.create_check_constraint(
        'ck_order_status_valid',
        'orders',
        "status IN ('pending_payment', 'confirmed', 'packed', "
        "'out_for_delivery', 'delivered', 'cancelled', 'refunded')",
    )
    op.create_check_constraint(
        'ck_order_event_status_valid',
        'order_events',
        "status IN ('pending_payment', 'confirmed', 'packed', "
        "'out_for_delivery', 'delivered', 'cancelled', 'refunded')",
    )
    op.create_check_constraint(
        'ck_payment_method_valid', 'payments', "method IN ('online', 'cod')",
    )
    op.create_check_constraint(
        'ck_payment_status_valid',
        'payments',
        "status IN ('created', 'authorized', 'captured', 'failed', "
        "'refunded', 'refund_pending', 'amount_mismatch')",
    )


def downgrade() -> None:
    op.drop_constraint('ck_payment_status_valid', 'payments', type_='check')
    op.drop_constraint('ck_payment_method_valid', 'payments', type_='check')
    op.drop_constraint('ck_order_event_status_valid', 'order_events', type_='check')
    op.drop_constraint('ck_order_status_valid', 'orders', type_='check')
    op.drop_constraint('ck_variant_stock_policy_valid', 'variants', type_='check')
    op.drop_constraint('ck_user_role_valid', 'users', type_='check')

    op.drop_constraint('ck_variant_mrp_above_price', 'variants', type_='check')
    op.create_check_constraint(
        'ck_variant_mrp_above_price',
        'variants',
        'mrp_paise IS NULL OR mrp_paise > price_paise',
    )
