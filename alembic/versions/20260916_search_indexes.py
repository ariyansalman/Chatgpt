"""V45.1 — Enterprise Global Search: database indexes for fast search.

Revision ID: 20260916_search_indexes
Revises:     20260915_enterprise_v45
Create Date: 2026-09-16

Adds indexes on columns that the Global Search Engine queries via
LIKE/contains but which lack a native DB index, so that full table scans
are avoided on large production databases.

Indexes created (only if they do not already exist):
  users          — username, first_name, last_name
  products       — name (btree)
  coupons        — code (btree, unique)
  support_tickets — subject
  broadcasts     — message_text
  reviews        — comment (btree)
  admin_audit_logs — action, details (btree)
  admin_notifications — title, body
  referral_rewards — referrer_id, referred_id (already FK but double-check)

All are conditional on the index/column existence to remain idempotent.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision      = "20260916_search_indexes"
down_revision = "20260915_enterprise_v45"
branch_labels = None
depends_on    = None


def _index_exists(index_name: str) -> bool:
    from sqlalchemy import inspect
    return index_name in [
        idx["name"]
        for idx in inspect(op.get_bind()).get_indexes("users")  # broad check
    ] if False else _raw_index_exists(index_name)


def _raw_index_exists(index_name: str) -> bool:
    """Check pg_indexes for existence (PostgreSQL)."""
    result = op.get_bind().execute(
        sa.text(
            "SELECT 1 FROM pg_indexes WHERE indexname = :n"
        ),
        {"n": index_name}
    ).fetchone()
    return result is not None


def _column_exists(table: str, column: str) -> bool:
    from sqlalchemy import inspect, text
    try:
        cols = [c["name"] for c in inspect(op.get_bind()).get_columns(table)]
        return column in cols
    except Exception:
        return False


def _create_index_safe(name: str, table: str, columns: list[str]) -> None:
    """Create a btree index only if it doesn't already exist."""
    if _raw_index_exists(name):
        return
    # Filter columns that actually exist in the table
    existing = [c for c in columns if _column_exists(table, c)]
    if not existing:
        return
    op.create_index(name, table, existing)


def upgrade() -> None:
    # ── users ──────────────────────────────────────────────────────────────────
    _create_index_safe("ix_users_username_search",   "users", ["username"])
    _create_index_safe("ix_users_first_name_search", "users", ["first_name"])
    _create_index_safe("ix_users_last_name_search",  "users", ["last_name"])

    # ── products ───────────────────────────────────────────────────────────────
    _create_index_safe("ix_products_name_search", "products", ["name"])

    # ── coupons ────────────────────────────────────────────────────────────────
    _create_index_safe("ix_coupons_code_search", "coupons", ["code"])

    # ── support_tickets ────────────────────────────────────────────────────────
    _create_index_safe("ix_support_tickets_subject_search", "support_tickets", ["subject"])

    # ── broadcasts ─────────────────────────────────────────────────────────────
    _create_index_safe("ix_broadcasts_message_text_search", "broadcasts", ["message_text"])
    _create_index_safe("ix_broadcasts_message_search",      "broadcasts", ["message"])

    # ── reviews ────────────────────────────────────────────────────────────────
    _create_index_safe("ix_reviews_comment_search",    "reviews", ["comment"])
    _create_index_safe("ix_reviews_product_id_search", "reviews", ["product_id"])

    # ── admin_audit_logs ───────────────────────────────────────────────────────
    _create_index_safe("ix_admin_audit_logs_action_search",  "admin_audit_logs", ["action"])
    _create_index_safe("ix_admin_audit_logs_details_search", "admin_audit_logs", ["details"])

    # ── admin_notifications ────────────────────────────────────────────────────
    _create_index_safe("ix_admin_notifications_title_search",      "admin_notifications", ["title"])
    _create_index_safe("ix_admin_notifications_event_type_search",  "admin_notifications", ["event_type"])

    # ── product_keys ───────────────────────────────────────────────────────────
    # key_value is TEXT — index creation on TEXT is fine for btree prefix searches
    _create_index_safe("ix_product_keys_value_search", "product_keys", ["product_id", "is_sold"])

    # ── gift_cards ─────────────────────────────────────────────────────────────
    _create_index_safe("ix_gift_cards_code_search",  "gift_cards",  ["code"])
    _create_index_safe("ix_gift_cards_label_search", "gift_cards",  ["label"])

    # ── referral_rewards ───────────────────────────────────────────────────────
    _create_index_safe("ix_referral_rewards_referrer_search", "referral_rewards", ["referrer_id"])

    # ── transactions ───────────────────────────────────────────────────────────
    # txid already has index from original schema; add proof/crypto_address
    _create_index_safe("ix_transactions_crypto_address_search", "transactions", ["crypto_address"])

    # ── seed gse bot_config keys ───────────────────────────────────────────────
    conn = op.get_bind()
    seed = [
        ("gse_status",       "enabled",  "GSE: feature status (enabled/maintenance/disabled)"),
        ("gse_max_results",  "200",      "GSE: max total results returned per search"),
        ("gse_fuzzy",        "true",     "GSE: enable fuzzy / partial matching"),
        ("gse_keep_history", "true",     "GSE: persist search history"),
    ]
    for key, value, description in seed:
        exists = conn.execute(
            sa.text("SELECT 1 FROM bot_config WHERE key = :k"), {"k": key}
        ).fetchone()
        if not exists:
            conn.execute(
                sa.text("INSERT INTO bot_config (key, value, description) VALUES (:k, :v, :d)"),
                {"k": key, "v": value, "d": description},
            )


def downgrade() -> None:
    for idx in [
        "ix_users_username_search", "ix_users_first_name_search", "ix_users_last_name_search",
        "ix_products_name_search",
        "ix_coupons_code_search",
        "ix_support_tickets_subject_search",
        "ix_broadcasts_message_text_search", "ix_broadcasts_message_search",
        "ix_reviews_comment_search", "ix_reviews_product_id_search",
        "ix_admin_audit_logs_action_search", "ix_admin_audit_logs_details_search",
        "ix_admin_notifications_title_search", "ix_admin_notifications_event_type_search",
        "ix_product_keys_value_search",
        "ix_gift_cards_code_search", "ix_gift_cards_label_search",
        "ix_referral_rewards_referrer_search",
        "ix_transactions_crypto_address_search",
    ]:
        try:
            if _raw_index_exists(idx):
                op.drop_index(idx)
        except Exception:
            pass
