"""Add private life sources, interview and autobiography tables."""

from importlib import import_module

from alembic import op

from findyou.db import Base

revision = "0004_life_model"
down_revision = "0003_social_agent"
branch_labels = None
depends_on = None


def upgrade():
    for module in ("life_models", "life_story_models"):
        import_module("findyou." + module)
    tables = [table for table in Base.metadata.sorted_tables
              if table.name.startswith("life_")]
    Base.metadata.create_all(op.get_bind(), tables=tables, checkfirst=True)


def downgrade():
    raise RuntimeError("Destructive schema downgrade is unavailable; restore a verified backup")
