"""Persist extraction identity without replacing corrected or duplicate legacy facts."""

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "0002_proposal_identity"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    # 0001 creates current metadata on fresh installations; older 0001 databases
    # need the additive column/index. Never remove legacy facts or user edits.
    if "proposal_key" not in {c["name"] for c in sa.inspect(connection).get_columns("memories")}:
        op.add_column("memories", sa.Column("proposal_key", sa.String(64), nullable=True))
    if "uq_memory_proposal_key" not in {i["name"] for i in sa.inspect(connection).get_indexes("memories")}:
        op.create_index("uq_memory_proposal_key", "memories", ["proposal_key"], unique=True)
    rows = connection.execute(sa.text("""
        SELECT m.id,m.owner_id,v.id AS version_id,v.text,v.kind FROM memories m
        JOIN memory_versions v ON v.memory_id=m.id AND v.version=1
        WHERE m.proposal_key IS NULL
        ORDER BY m.current_version DESC,CASE m.status WHEN 'confirmed' THEN 0 ELSE 1 END,m.created_at,m.id
    """)).mappings().all()
    for row in rows:
        blocks = connection.execute(sa.text(
            "SELECT block_id FROM memory_evidence WHERE version_id=:version_id"
        ), {"version_id": row["version_id"]}).scalars().all()
        if not blocks:
            continue
        # Frozen identity v1: owner, sorted distinct source blocks, original text, original kind.
        value = json.dumps([row["owner_id"], sorted(set(blocks)), row["text"].strip(), row["kind"].strip()],
                           ensure_ascii=False, separators=(",", ":"))
        key = hashlib.sha256(value.encode("utf-8")).hexdigest()
        existing = connection.scalar(sa.text("SELECT id FROM memories WHERE proposal_key=:key"), {"key": key})
        if existing is None:
            connection.execute(sa.text("UPDATE memories SET proposal_key=:key WHERE id=:id"),
                               {"key": key, "id": row["id"]})


def downgrade():
    raise RuntimeError("Destructive schema downgrade is not supported; restore a verified backup instead.")
