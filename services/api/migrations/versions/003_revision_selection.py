"""Preserve reclassification evidence without rewriting archived revisions."""

from alembic import op

revision = "003"
down_revision = "002"


def upgrade():
    # All previously admitted records passed the earthquake/source selection.
    # Add metadata defaults without changing their values, IDs, or content hashes.
    op.execute("ALTER TABLE earthquake_revisions ADD COLUMN event_type VARCHAR NOT NULL DEFAULT 'earthquake'")
    op.execute("ALTER TABLE earthquake_revisions ADD COLUMN selection_reason VARCHAR")
    op.execute(
        "ALTER TABLE earthquake_revisions ADD COLUMN hash_parent_id VARCHAR(36) "
        "REFERENCES earthquake_revisions(id)"
    )


def downgrade():
    raise RuntimeError("Restore a verified backup instead of removing archived selection evidence")
