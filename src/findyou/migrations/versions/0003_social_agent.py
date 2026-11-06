"""Add source-linked social identities, persona memory, and bounded agent runs."""

from importlib import import_module

from alembic import op

from findyou.db import Base

revision = "0003_social_agent"
down_revision = "0002_proposal_identity"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("content_models", "ops", "social_models", "identity_models", "agent_models"):
        import_module("findyou." + name)
    # Additive tables only. checkfirst also handles fresh 0001 metadata installations.
    Base.metadata.create_all(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Destructive schema downgrade is unavailable; restore a verified backup")
