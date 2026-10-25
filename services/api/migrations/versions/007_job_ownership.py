"""Fence newly claimed attempts without rewriting earlier job history."""

from alembic import op

revision = "007"
down_revision = "006"


def upgrade():
    op.execute("""
        ALTER TABLE jobs ADD COLUMN attempt_token VARCHAR(36);
        ALTER TABLE jobs ADD COLUMN worker_token VARCHAR(36);
        ALTER TABLE jobs ADD CONSTRAINT job_attempt_owner_pair
          CHECK ((attempt_token IS NULL) = (worker_token IS NULL));
    """)


def downgrade():
    raise RuntimeError("Restore a verified backup instead of deleting job ownership history")
