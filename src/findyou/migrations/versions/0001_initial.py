"""Initial content and operational schema."""

from importlib import import_module

from alembic import op

from findyou.db import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    import_module("findyou.content_models")
    import_module("findyou.ops")
    Base.metadata.create_all(op.get_bind())
    op.execute("CREATE VIRTUAL TABLE memory_fts USING fts5(memory_id UNINDEXED, text, tokenize='trigram')")


def downgrade():
    raise RuntimeError("Destructive schema downgrade is not supported; restore a verified backup instead.")
