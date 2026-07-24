"""V21 Six Features — stub migration (revision file restored).

This revision was applied to the database but its migration file was
lost during a merge/rename. This stub satisfies the alembic revision
chain without repeating any DDL (all schema changes it originally
introduced were subsequently re-applied via later migrations or
SQLAlchemy create_all()).

Revision ID: 20260811_v21_six_features
Revises: 20260810_advanced_features
Create Date: 2026-08-11
"""

from alembic import op
import sqlalchemy as sa

revision = "20260811_v21_six_features"
down_revision = "20260810_advanced_features"
branch_labels = None
depends_on = None


def upgrade():
    # No-op stub — the schema changes originally in this revision were
    # re-applied via later migrations and/or SQLAlchemy's create_all().
    pass


def downgrade():
    # No-op stub — downgrade would remove features added in V21 but since
    # the DDL was re-applied via other mechanisms, this is intentionally empty.
    pass
