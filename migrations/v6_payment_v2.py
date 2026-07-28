"""Migration v6 (Payment v2): extend manual payment methods + transactions.

Adds to `manual_payment_methods`:
  - account_label      VARCHAR(120)  NULL
  - account_number     VARCHAR(255)  NULL
  - max_amount         FLOAT         NULL   (NULL / 0 = no ceiling)
  - require_txid       BOOLEAN       DEFAULT 1
  - require_proof      BOOLEAN       DEFAULT 1
  - updated_at         DATETIME      DEFAULT CURRENT_TIMESTAMP   (v3 rows may miss it)

Adds to `transactions`:
  - txid               VARCHAR(128)  NULL   (indexed)
  - proof_file_id      VARCHAR(256)  NULL

Also backfills `transactions.proof_file_id` from any legacy
`crypto_address` value of the form `photo:<file_id>` so older manual payments
continue to render correctly in the admin panel.

Idempotent — safe to run multiple times. Works on SQLite and PostgreSQL.

Usage:
    python -m migrations.v6_payment_v2
"""

from sqlalchemy import inspect, text

from database.db import engine
from database import Base  # ensures all models are imported


def _has_column(inspector, table, column):
    return any(c["name"] == column for c in inspector.get_columns(table))


def _add_column(conn, dialect, table, column, sql_type, default_sql=None):
    ddl = f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
    if default_sql is not None:
        ddl += f" DEFAULT {default_sql}"
    conn.execute(text(ddl))


def run():
    inspector = inspect(engine)
    dialect = engine.dialect.name
    bool_true = "1" if dialect == "sqlite" else "TRUE"
    float_type = "FLOAT" if dialect == "sqlite" else "DOUBLE PRECISION"

    with engine.begin() as conn:
        # ── manual_payment_methods: new admin-editable fields ──────────
        if "manual_payment_methods" not in inspector.get_table_names():
            print("⚠ manual_payment_methods table missing — run app once (or v3 migration) first.")
            return

        mpm_cols = [
            ("account_label",  "VARCHAR(120)", None),
            ("account_number", "VARCHAR(255)", None),
            ("max_amount",     float_type,     None),
            ("require_txid",   "BOOLEAN",      bool_true),
            ("require_proof",  "BOOLEAN",      bool_true),
            ("updated_at",     "DATETIME" if dialect == "sqlite" else "TIMESTAMP",
                               "CURRENT_TIMESTAMP"),
        ]
        for col, sql_type, default_sql in mpm_cols:
            if not _has_column(inspector, "manual_payment_methods", col):
                print(f"• Adding manual_payment_methods.{col} …")
                _add_column(conn, dialect, "manual_payment_methods", col, sql_type, default_sql)

        # ── transactions: TXID + proof_file_id ─────────────────────────
        inspector = inspect(engine)  # refresh after DDL
        if "transactions" not in inspector.get_table_names():
            print("⚠ transactions table missing — run app once to create schema.")
            return

        if not _has_column(inspector, "transactions", "txid"):
            print("• Adding transactions.txid …")
            _add_column(conn, dialect, "transactions", "txid", "VARCHAR(128)")
            # Non-unique index — uniqueness is enforced per-method in code
            # (some providers legitimately reuse TXIDs across accounts).
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_transactions_txid ON transactions (txid)"))

        if not _has_column(inspector, "transactions", "proof_file_id"):
            print("• Adding transactions.proof_file_id …")
            _add_column(conn, dialect, "transactions", "proof_file_id", "VARCHAR(256)")
            # Backfill from legacy crypto_address value 'photo:<file_id>'.
            print("• Backfilling proof_file_id from legacy crypto_address …")
            conn.execute(text(
                "UPDATE transactions "
                "SET proof_file_id = SUBSTR(crypto_address, 7) "
                "WHERE crypto_address LIKE 'photo:%' "
                "  AND (proof_file_id IS NULL OR proof_file_id = '')"
            ))

    print("✅ Migration v6 (payment_v2) complete.")


if __name__ == "__main__":
    run()