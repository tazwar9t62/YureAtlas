"""Separate durable scheduling decisions from worker heartbeat updates."""

from alembic import op

revision = "006"
down_revision = "005"


def upgrade():
    op.execute("""
        CREATE TABLE scheduler_state (
            id VARCHAR PRIMARY KEY,
            reconciled_at TIMESTAMP WITH TIME ZONE NOT NULL,
            observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
            details JSONB NOT NULL,
            CHECK (reconciled_at >= observed_at)
        );
    """)


def downgrade():
    raise RuntimeError("Restore a verified backup instead of deleting scheduling decisions")
