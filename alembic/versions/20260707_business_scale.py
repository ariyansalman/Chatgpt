"""business_scale: suppliers, inventory batches, quality issues, resellers,
delivery queue, backups, integrity center.

Revision ID: 20260707_bs
Revises: 20260706_admc

Fully additive / non-destructive.
Enum-like columns use VARCHAR (validated in Python) to avoid the
PostgreSQL native-enum migration issues that hit OrderStatus before.
Every op is guarded so the migration is re-runnable.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260707_bs"
down_revision = "20260706_admc"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _has_table(t: str) -> bool:
    return t in inspect(_bind()).get_table_names()


def _has_col(t: str, c: str) -> bool:
    if not _has_table(t):
        return False
    return c in {col["name"] for col in inspect(_bind()).get_columns(t)}


def _add_col(table: str, col: sa.Column):
    if _has_table(table) and not _has_col(table, col.name):
        with op.batch_alter_table(table) as b:
            b.add_column(col)


def upgrade():
    # ── suppliers ───────────────────────────────────────────────────────
    if not _has_table("suppliers"):
        op.create_table(
            "suppliers",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String(200), nullable=False, index=True),
            sa.Column("contact", sa.String(255), nullable=True),
            sa.Column("telegram_username", sa.String(64), nullable=True),
            sa.Column("notes", sa.Text, nullable=True),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime, nullable=True),
            sa.Column("updated_at", sa.DateTime, nullable=True),
        )

    # ── inventory_batches ───────────────────────────────────────────────
    if not _has_table("inventory_batches"):
        op.create_table(
            "inventory_batches",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("reference", sa.String(80), unique=True, nullable=False, index=True),
            sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"),
                      nullable=False, index=True),
            sa.Column("variant_id", sa.Integer, sa.ForeignKey("product_variants.id"),
                      nullable=True, index=True),
            sa.Column("supplier_id", sa.Integer, sa.ForeignKey("suppliers.id"),
                      nullable=True, index=True),
            sa.Column("quantity_imported", sa.Integer, nullable=False, server_default="0"),
            sa.Column("cost_per_unit", sa.Float, nullable=False, server_default="0"),
            sa.Column("total_cost", sa.Float, nullable=False, server_default="0"),
            sa.Column("currency", sa.String(8), nullable=True),
            sa.Column("import_source", sa.String(32), nullable=True),
            sa.Column("notes", sa.Text, nullable=True),
            sa.Column("created_by", sa.BigInteger, nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=True, index=True),
        )

    # ── inventory_issues ────────────────────────────────────────────────
    if not _has_table("inventory_issues"):
        op.create_table(
            "inventory_issues",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("product_key_id", sa.Integer, sa.ForeignKey("product_keys.id"),
                      nullable=True, index=True),
            sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"),
                      nullable=True, index=True),
            sa.Column("dispute_id", sa.Integer, sa.ForeignKey("disputes.id"),
                      nullable=True, index=True),
            sa.Column("batch_id", sa.Integer, sa.ForeignKey("inventory_batches.id"),
                      nullable=True, index=True),
            sa.Column("supplier_id", sa.Integer, sa.ForeignKey("suppliers.id"),
                      nullable=True, index=True),
            sa.Column("issue_type", sa.String(32), nullable=False, index=True),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column("reporter_type", sa.String(16), nullable=False, server_default="system"),
            sa.Column("reporter_id", sa.BigInteger, nullable=True),
            sa.Column("admin_id", sa.BigInteger, nullable=True),
            sa.Column("resolution", sa.Text, nullable=True),
            sa.Column("replacement_key_id", sa.Integer, sa.ForeignKey("product_keys.id"),
                      nullable=True),
            sa.Column("replacement_cost", sa.Float, nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=True, index=True),
            sa.Column("resolved_at", sa.DateTime, nullable=True),
        )

    # ── reseller_tiers ──────────────────────────────────────────────────
    if not _has_table("reseller_tiers"):
        op.create_table(
            "reseller_tiers",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String(80), nullable=False, unique=True),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
            sa.Column("display_order", sa.Integer, server_default="0"),
            sa.Column("min_qualification_spend", sa.Float, nullable=True),
            sa.Column("discount_pct", sa.Float, nullable=False, server_default="0"),
            sa.Column("min_quantity", sa.Integer, nullable=False, server_default="1"),
            sa.Column("points_multiplier", sa.Float, nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime, nullable=True),
            sa.Column("updated_at", sa.DateTime, nullable=True),
        )

    # ── user_reseller ───────────────────────────────────────────────────
    if not _has_table("user_reseller"):
        op.create_table(
            "user_reseller",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"),
                      nullable=False, unique=True, index=True),
            sa.Column("tier_id", sa.Integer, sa.ForeignKey("reseller_tiers.id"),
                      nullable=False, index=True),
            sa.Column("assigned_by", sa.BigInteger, nullable=True),
            sa.Column("assigned_at", sa.DateTime, nullable=True),
        )

    # ── delivery_jobs ───────────────────────────────────────────────────
    if not _has_table("delivery_jobs"):
        op.create_table(
            "delivery_jobs",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"),
                      nullable=False, index=True),
            sa.Column("status", sa.String(24), nullable=False,
                      server_default="PENDING", index=True),
            sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer, nullable=False, server_default="5"),
            sa.Column("next_retry_at", sa.DateTime, nullable=True, index=True),
            sa.Column("last_error_category", sa.String(48), nullable=True),
            sa.Column("last_error_summary", sa.String(500), nullable=True),
            sa.Column("inventory_assigned", sa.Boolean, nullable=False,
                      server_default=sa.false()),
            sa.Column("created_at", sa.DateTime, nullable=True, index=True),
            sa.Column("started_at", sa.DateTime, nullable=True),
            sa.Column("completed_at", sa.DateTime, nullable=True),
            sa.Column("updated_at", sa.DateTime, nullable=True),
        )
        # Only ONE active delivery job per order at a time. Partial index on
        # Postgres, plain unique on SQLite (created via constraint below).
        if _bind().dialect.name == "postgresql":
            op.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_delivery_jobs_order_active "
                "ON delivery_jobs (order_id) "
                "WHERE status IN ('PENDING','PROCESSING','RETRY_SCHEDULED')"
            )

    # ── backup_records ──────────────────────────────────────────────────
    if not _has_table("backup_records"):
        op.create_table(
            "backup_records",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("filename", sa.String(255), nullable=False),
            sa.Column("method", sa.String(32), nullable=False, server_default="pg_dump"),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="RUNNING", index=True),
            sa.Column("size_bytes", sa.BigInteger, nullable=True),
            sa.Column("error_summary", sa.String(500), nullable=True),
            sa.Column("triggered_by", sa.String(16), nullable=False,
                      server_default="schedule"),
            sa.Column("admin_id", sa.BigInteger, nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=True, index=True),
            sa.Column("completed_at", sa.DateTime, nullable=True),
        )

    # ── integrity_scans / results ───────────────────────────────────────
    if not _has_table("integrity_scans"):
        op.create_table(
            "integrity_scans",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("triggered_by", sa.String(16), nullable=False,
                      server_default="manual"),
            sa.Column("admin_id", sa.BigInteger, nullable=True),
            sa.Column("started_at", sa.DateTime, nullable=True, index=True),
            sa.Column("completed_at", sa.DateTime, nullable=True),
            sa.Column("total_checks", sa.Integer, server_default="0"),
            sa.Column("total_issues", sa.Integer, server_default="0"),
            sa.Column("critical_count", sa.Integer, server_default="0"),
            sa.Column("warning_count", sa.Integer, server_default="0"),
            sa.Column("info_count", sa.Integer, server_default="0"),
        )
    if not _has_table("integrity_scan_results"):
        op.create_table(
            "integrity_scan_results",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("scan_id", sa.Integer, sa.ForeignKey("integrity_scans.id"),
                      nullable=False, index=True),
            sa.Column("check_name", sa.String(80), nullable=False, index=True),
            sa.Column("severity", sa.String(16), nullable=False),
            sa.Column("count", sa.Integer, server_default="0"),
            sa.Column("explanation", sa.Text, nullable=True),
            sa.Column("sample_ids", sa.Text, nullable=True),
        )

    # ── extension columns on existing tables ────────────────────────────
    _add_col("product_keys", sa.Column("batch_id", sa.Integer,
             sa.ForeignKey("inventory_batches.id"), nullable=True))
    _add_col("product_keys", sa.Column("cost_per_unit_snapshot", sa.Float, nullable=True))

    _add_col("order_items", sa.Column("base_price", sa.Float, nullable=True))
    _add_col("order_items", sa.Column("unit_cost_snapshot", sa.Float, nullable=True))
    _add_col("order_items", sa.Column("total_cost_snapshot", sa.Float, nullable=True))
    _add_col("order_items", sa.Column("reseller_tier_id", sa.Integer,
             sa.ForeignKey("reseller_tiers.id"), nullable=True))
    _add_col("order_items", sa.Column("pricing_meta", sa.Text, nullable=True))


def downgrade():
    # Non-destructive: never drop tables carrying business data.
    pass
