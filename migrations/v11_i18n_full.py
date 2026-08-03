"""One-shot migration extending v2_add_referral_support_i18n's i18n base.

v2 already added `users.language VARCHAR(8) DEFAULT 'en'` but nothing wrote
to it (English-only build). This migration is safe to re-run and:

  1. Ensures `users.language` exists (in case v2 never ran on this DB).
  2. Backfills any NULL / blank / unsupported language values to 'en' so
     every row has a valid, supported code the i18n module can trust.

Run once after upgrading the code:
    python -m migrations.v11_i18n_full
"""

from sqlalchemy import inspect, text
from database.db import engine

SUPPORTED_LANGUAGES = ("en", "bn")


def _has_column(inspector, table: str, column: str) -> bool:
    try:
        cols = [c["name"] for c in inspector.get_columns(table)]
        return column in cols
    except Exception:
        return False


def run():
    insp = inspect(engine)
    dialect = engine.dialect.name
    print(f"[migrate v11] dialect={dialect}")

    with engine.begin() as conn:
        if not _has_column(insp, "users", "language"):
            try:
                conn.execute(text("ALTER TABLE users ADD COLUMN language VARCHAR(8) DEFAULT 'en'"))
                print("[migrate v11] +users.language")
            except Exception as e:
                print(f"[migrate v11] failed to add users.language: {e}")

        # Backfill NULL/blank/unsupported -> 'en'
        placeholders = ", ".join(f"'{code}'" for code in SUPPORTED_LANGUAGES)
        try:
            result = conn.execute(text(
                f"UPDATE users SET language = 'en' "
                f"WHERE language IS NULL OR TRIM(language) = '' OR language NOT IN ({placeholders})"
            ))
            print(f"[migrate v11] backfilled {result.rowcount if result.rowcount is not None else '?'} row(s) to 'en'")
        except Exception as e:
            print(f"[migrate v11] backfill skipped: {e}")

    print("[migrate v11] done")


if __name__ == "__main__":
    run()
