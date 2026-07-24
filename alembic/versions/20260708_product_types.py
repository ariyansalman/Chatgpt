"""product_types_360: extend ProductType enum + 8 new supporting tables
+ extra Product columns (type_config, warranty_info, telegram_file_id, ...).

Revision ID: 20260708_pt360
Revises: 20260707_bs

Fully additive / non-destructive.
- On PostgreSQL: extends the native ``producttype`` enum with the 10 new
  member names via ALTER TYPE ... ADD VALUE IF NOT EXISTS.
- On SQLite: no enum type exists, no-op.
- Every op is guarded so the migration is re-runnable.
"""
import logging

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

logger = logging.getLogger(__name__)

revision = "20260708_pt360"
down_revision = "20260707_bs"
branch_labels = None
depends_on = None


NEW_ENUM_MEMBERS = [
    "REDEEM_LINK", "ACCOUNT_LOGIN", "DOWNLOADABLE_FILE", "AUTO_GENERATED",
    "MANUAL_DELIVERY", "PREORDER", "SUBSCRIPTION", "BUNDLE", "SERVICE",
    "VOUCHER", "EXTERNAL_DELIVERY",
]


def _bind():
    return op.get_bind()


def _dialect():
    return _bind().dialect.name


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
    # ── 1) Extend ProductType enum on PostgreSQL ────────────────────────
    if _dialect() == "postgresql":
        conn = _bind()
        # ALTER TYPE ADD VALUE requires no open transaction (Postgres restriction).
        # Commit the alembic-managed transaction first, then run DDL directly.
        conn.execute(sa.text("COMMIT"))
        for member in NEW_ENUM_MEMBERS:
            # NOTE: `ALTER TYPE ... ADD VALUE` does not accept a bound
            # parameter for the value — PostgreSQL's DDL grammar only
            # accepts a literal there, so `.bindparams(val=member)` fails
            # with a syntax error on every run. `member` is one of this
            # module's own hardcoded constants above (never user input),
            # so a literal is safe here.
            try:
                conn.execute(
                    sa.text(f"ALTER TYPE producttype ADD VALUE IF NOT EXISTS '{member}'")
                )
            except Exception as e:
                msg = str(e).lower()
                if "already exists" in msg or "duplicate" in msg:
                    pass
                else:
                    # Enum type may be named differently in old installs —
                    # log it instead of swallowing silently, so a real
                    # failure here doesn't leave a member permanently
                    # missing from production without anyone noticing.
                    logger.warning("Could not add '%s' to producttype enum: %s", member, e)

    # ── 2) Add new Product columns ──────────────────────────────────────
    _add_col("products", sa.Column("type_config", sa.Text, nullable=True))
    _add_col("products", sa.Column("delivery_note", sa.Text, nullable=True))
    _add_col("products", sa.Column("warranty_info", sa.Text, nullable=True))
    _add_col("products", sa.Column("min_quantity", sa.Integer, nullable=True))
    _add_col("products", sa.Column("max_quantity", sa.Integer, nullable=True))
    _add_col("products", sa.Column("bulk_purchase_enabled", sa.Boolean,
                                   nullable=False, server_default=sa.true()))
    _add_col("products", sa.Column("telegram_file_id", sa.String(256), nullable=True))
    _add_col("products", sa.Column("telegram_file_type", sa.String(24), nullable=True))
    _add_col("products", sa.Column("reusable", sa.Boolean,
                                   nullable=False, server_default=sa.false()))

    # ── 3) New tables ───────────────────────────────────────────────────
    if not _has_table("subscription_plans"):
        op.create_table(
            "subscription_plans",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"),
                      nullable=False, index=True),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("duration_days", sa.Integer, nullable=False, server_default="30"),
            sa.Column("price", sa.Float, nullable=False, server_default="0"),
            sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true(),
                      index=True),
            sa.Column("delivery_type", sa.String(24), nullable=True),
            sa.Column("renewal_instructions", sa.Text, nullable=True),
            sa.Column("display_order", sa.Integer, server_default="0"),
            sa.Column("created_at", sa.DateTime, nullable=True),
            sa.Column("updated_at", sa.DateTime, nullable=True),
        )

    if not _has_table("subscriptions"):
        op.create_table(
            "subscriptions",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"),
                      nullable=False, index=True),
            sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"),
                      nullable=False, index=True),
            sa.Column("plan_id", sa.Integer, sa.ForeignKey("subscription_plans.id"),
                      nullable=True),
            sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"),
                      nullable=True, index=True),
            sa.Column("starts_at", sa.DateTime, nullable=False),
            sa.Column("expires_at", sa.DateTime, nullable=False, index=True),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="active", index=True),
            sa.Column("created_at", sa.DateTime, nullable=True),
        )

    if not _has_table("bundle_items"):
        op.create_table(
            "bundle_items",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("parent_product_id", sa.Integer, sa.ForeignKey("products.id"),
                      nullable=False, index=True),
            sa.Column("child_product_id", sa.Integer, sa.ForeignKey("products.id"),
                      nullable=False, index=True),
            sa.Column("quantity", sa.Integer, nullable=False, server_default="1"),
            sa.Column("display_order", sa.Integer, server_default="0"),
            sa.Column("created_at", sa.DateTime, nullable=True),
        )

    if not _has_table("preorders"):
        op.create_table(
            "preorders",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"),
                      nullable=False, index=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"),
                      nullable=False, index=True),
            sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"),
                      nullable=False, index=True),
            sa.Column("quantity", sa.Integer, nullable=False, server_default="1"),
            sa.Column("status", sa.String(24), nullable=False,
                      server_default="pending", index=True),
            sa.Column("estimated_delivery", sa.String(255), nullable=True),
            sa.Column("admin_note", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=True),
            sa.Column("updated_at", sa.DateTime, nullable=True),
        )

    if not _has_table("service_orders"):
        op.create_table(
            "service_orders",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"),
                      nullable=False, index=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"),
                      nullable=False, index=True),
            sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"),
                      nullable=False, index=True),
            sa.Column("submitted_fields", sa.Text, nullable=True),
            sa.Column("status", sa.String(24), nullable=False,
                      server_default="pending", index=True),
            sa.Column("admin_note", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=True),
            sa.Column("updated_at", sa.DateTime, nullable=True),
        )

    if not _has_table("manual_delivery_tasks"):
        op.create_table(
            "manual_delivery_tasks",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"),
                      nullable=False, index=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"),
                      nullable=False, index=True),
            sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"),
                      nullable=False, index=True),
            sa.Column("quantity", sa.Integer, nullable=False, server_default="1"),
            sa.Column("status", sa.String(24), nullable=False,
                      server_default="pending", index=True),
            sa.Column("admin_note", sa.Text, nullable=True),
            sa.Column("delivery_payload", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=True),
            sa.Column("updated_at", sa.DateTime, nullable=True),
        )

    if not _has_table("external_integrations"):
        op.create_table(
            "external_integrations",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("name", sa.String(120), nullable=False, unique=True, index=True),
            sa.Column("endpoint_url", sa.String(500), nullable=False),
            sa.Column("http_method", sa.String(8), nullable=False,
                      server_default="POST"),
            sa.Column("auth_type", sa.String(24), nullable=False,
                      server_default="none"),
            sa.Column("credential_env_name", sa.String(80), nullable=True),
            sa.Column("timeout_seconds", sa.Integer, nullable=False,
                      server_default="30"),
            sa.Column("max_retries", sa.Integer, nullable=False, server_default="2"),
            sa.Column("request_template", sa.Text, nullable=True),
            sa.Column("response_mapping", sa.Text, nullable=True),
            sa.Column("is_active", sa.Boolean, nullable=False,
                      server_default=sa.true(), index=True),
            sa.Column("created_at", sa.DateTime, nullable=True),
            sa.Column("updated_at", sa.DateTime, nullable=True),
        )

    if not _has_table("generated_values"):
        op.create_table(
            "generated_values",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("product_id", sa.Integer, sa.ForeignKey("products.id"),
                      nullable=False, index=True),
            sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"),
                      nullable=False, index=True),
            sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"),
                      nullable=False, index=True),
            sa.Column("value", sa.String(255), nullable=False, unique=True, index=True),
            sa.Column("expires_at", sa.DateTime, nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=True),
        )

    if not _has_table("external_delivery_logs"):
        op.create_table(
            "external_delivery_logs",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("order_id", sa.Integer, sa.ForeignKey("orders.id"),
                      nullable=False, index=True),
            sa.Column("integration_id", sa.Integer,
                      sa.ForeignKey("external_integrations.id"), nullable=True),
            sa.Column("idempotency_key", sa.String(120), nullable=False,
                      unique=True, index=True),
            sa.Column("attempt", sa.Integer, nullable=False, server_default="1"),
            sa.Column("status", sa.String(24), nullable=False,
                      server_default="pending", index=True),
            sa.Column("http_status", sa.Integer, nullable=True),
            sa.Column("response_summary", sa.Text, nullable=True),
            sa.Column("delivered_value", sa.Text, nullable=True),
            sa.Column("error_summary", sa.String(500), nullable=True),
            sa.Column("created_at", sa.DateTime, nullable=True, index=True),
            sa.Column("completed_at", sa.DateTime, nullable=True),
        )


def downgrade():
    """Non-destructive downgrade — drops V11 tables only.

    ProductType enum values are intentionally NOT removed because Postgres
    cannot drop enum values without rewriting every table that uses them,
    which would corrupt live product rows.
    """
    for t in [
        "external_delivery_logs", "generated_values", "external_integrations",
        "manual_delivery_tasks", "service_orders", "preorders", "bundle_items",
        "subscriptions", "subscription_plans",
    ]:
        if _has_table(t):
            op.drop_table(t)

    for c in ["reusable", "telegram_file_type", "telegram_file_id",
              "bulk_purchase_enabled", "max_quantity", "min_quantity",
              "warranty_info", "delivery_note", "type_config"]:
        if _has_col("products", c):
            with op.batch_alter_table("products") as b:
                b.drop_column(c)
