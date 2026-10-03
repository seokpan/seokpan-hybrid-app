"""Expand game_participant with an application-owned participant identity.

Revision ID: 20260902_0002
Revises: 20260901_0001
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "20260902_0002"
down_revision: str | None = "20260901_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "game_participant",
        sa.Column("participant_id", mysql.CHAR(36), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("game_participant", "participant_id")
