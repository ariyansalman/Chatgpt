"""Multi-Currency Wallet System — V39

Adds:
  • wallet_currency_configs    — admin-managed currency registry
  • user_currency_wallets      — per-user per-currency balances
  • currency_transactions      — append-only multi-currency ledger

All new tables work side-by-side with the existing User.wallet_balance
(USD primary wallet) — zero changes to existing schema.

Revision ID: 20260903_multicurrency_wallet
Revises:     20260902_flash_sale_manager
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260903_multicurrency_wallet"
down_revision = "20260902_flash_sale_manager"
branch_labels = None
depends_on = None

_NEW_CONFIG_KEYS = [
    # Feature status
    ("multicurrency_wallet_status", "str", "enabled", "wallets",
     "Multi-Currency Wallet Status",
     "enabled = operational; maintenance = read-only; disabled = off."),
    # Per-currency defaults
    ("mcw_default_deposit_fee_pct", "float", "0.0", "wallets",
     "Default Deposit Fee (%)",
     "Default deposit fee percentage applied to new currencies."),
    ("mcw_default_withdrawal_fee_pct", "float", "0.0", "wallets",
     "Default Withdrawal Fee (%)",
     "Default withdrawal fee percentage applied to new currencies."),
    ("mcw_portfolio_display_currency", "str", "USD", "wallets",
     "Portfolio Display Currency",
     "Currency used to show total portfolio value (e.g. USD)."),
    ("mcw_transfer_enabled", "bool", "true", "wallets",
     "Enable Wallet-to-Wallet Transfer",
     "Allow users to transfer between their own currency wallets."),
    ("mcw_show_zero_balances", "bool", "true", "wallets",
     "Show Zero-Balance Wallets",
     "Show all enabled currencies in the wallet even if balance is 0."),
]


def upgrade() -> None:
    conn = op.get_bind()

    # ── wallet_currency_configs ───────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS wallet_currency_configs (
            id                   SERIAL PRIMARY KEY,
            code                 VARCHAR(16)  NOT NULL,
            name                 VARCHAR(64)  NOT NULL,
            symbol               VARCHAR(8)   NOT NULL DEFAULT '$',
            is_crypto            BOOLEAN      NOT NULL DEFAULT FALSE,
            is_enabled           BOOLEAN      NOT NULL DEFAULT TRUE,
            status               VARCHAR(16)  NOT NULL DEFAULT 'enabled',
            is_frozen            BOOLEAN      NOT NULL DEFAULT FALSE,
            min_balance          FLOAT        NOT NULL DEFAULT 0.0,
            max_balance          FLOAT        NOT NULL DEFAULT 0.0,
            min_deposit          FLOAT        NOT NULL DEFAULT 0.0,
            max_deposit          FLOAT        NOT NULL DEFAULT 0.0,
            deposit_fee_pct      FLOAT        NOT NULL DEFAULT 0.0,
            min_withdrawal       FLOAT        NOT NULL DEFAULT 0.0,
            max_withdrawal       FLOAT        NOT NULL DEFAULT 0.0,
            withdrawal_fee_pct   FLOAT        NOT NULL DEFAULT 0.0,
            withdrawal_fee_flat  FLOAT        NOT NULL DEFAULT 0.0,
            sort_order           INTEGER      NOT NULL DEFAULT 0,
            notes                TEXT,
            created_at           TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at           TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_wcc_code UNIQUE (code)
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_wcc_code "
        "ON wallet_currency_configs (code)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_wcc_is_enabled "
        "ON wallet_currency_configs (is_enabled)"
    ))

    # ── Seed default currencies ───────────────────────────────────────────────
    defaults = [
        ("USD",  "US Dollar",         "$",   False, 1),
        ("BDT",  "Bangladeshi Taka",  "৳",  False, 2),
        ("USDT", "Tether",            "₮",  True,  3),
        ("BTC",  "Bitcoin",           "₿",  True,  4),
        ("ETH",  "Ethereum",          "Ξ",  True,  5),
        ("LTC",  "Litecoin",          "Ł",  True,  6),
        ("BNB",  "BNB",               "BNB", True,  7),
        ("TRX",  "TRON",              "TRX", True,  8),
    ]
    for code, name, symbol, is_crypto, sort_order in defaults:
        conn.execute(sa.text("""
            INSERT INTO wallet_currency_configs
                (code, name, symbol, is_crypto, sort_order, status, is_enabled)
            VALUES (:code, :name, :symbol, :is_crypto, :sort, 'enabled', TRUE)
            ON CONFLICT (code) DO NOTHING
        """), {"code": code, "name": name, "symbol": symbol,
               "is_crypto": is_crypto, "sort": sort_order})

    # ── user_currency_wallets ─────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS user_currency_wallets (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES users(id),
            currency_code   VARCHAR(16) NOT NULL,
            balance         FLOAT NOT NULL DEFAULT 0.0,
            is_frozen       BOOLEAN NOT NULL DEFAULT FALSE,
            created_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_ucw_user_currency UNIQUE (user_id, currency_code)
        )
    """))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_ucw_user_id "
        "ON user_currency_wallets (user_id)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_ucw_currency_code "
        "ON user_currency_wallets (currency_code)"
    ))

    # ── currency_transactions ─────────────────────────────────────────────────
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS currency_transactions (
            id              SERIAL PRIMARY KEY,
            user_id         INTEGER NOT NULL REFERENCES users(id),
            wallet_id       INTEGER NOT NULL REFERENCES user_currency_wallets(id),
            currency_code   VARCHAR(16) NOT NULL,
            tx_type         VARCHAR(32) NOT NULL,
            amount          FLOAT NOT NULL,
            fee             FLOAT NOT NULL DEFAULT 0.0,
            net_amount      FLOAT NOT NULL,
            balance_before  FLOAT NOT NULL,
            balance_after   FLOAT NOT NULL,
            status          VARCHAR(16) NOT NULL DEFAULT 'completed',
            ref_type        VARCHAR(32),
            ref_id          VARCHAR(64),
            actor_type      VARCHAR(16) NOT NULL DEFAULT 'system',
            actor_id        BIGINT,
            notes           VARCHAR(255),
            created_at      TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW()
        )
    """))
    for col, idx in [
        ("user_id",       "ix_curtx_user_id"),
        ("wallet_id",     "ix_curtx_wallet_id"),
        ("currency_code", "ix_curtx_currency_code"),
        ("created_at",    "ix_curtx_created_at"),
    ]:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS {idx} ON currency_transactions ({col})"
        ))

    # ── bot_config keys ───────────────────────────────────────────────────────
    for key, typ, val, cat, label, desc in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("""
            INSERT INTO bot_config (key, value_type, value, category, label, description)
            VALUES (:key, :type, :value, :category, :label, :desc)
            ON CONFLICT (key) DO NOTHING
        """), {"key": key, "type": typ, "value": val,
               "category": cat, "label": label, "desc": desc})


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS currency_transactions"))
    conn.execute(sa.text("DROP TABLE IF EXISTS user_currency_wallets"))
    conn.execute(sa.text("DROP TABLE IF EXISTS wallet_currency_configs"))
    for row in _NEW_CONFIG_KEYS:
        conn.execute(sa.text("DELETE FROM bot_config WHERE key = :key"), {"key": row[0]})
