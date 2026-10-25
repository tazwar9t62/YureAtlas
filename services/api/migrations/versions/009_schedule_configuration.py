"""Audit local schedule requests separately from the worker's applied state."""

from alembic import op

revision = "009"
down_revision = "008"


def upgrade():
    op.execute("""
        CREATE TABLE schedule_configurations (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            revision INTEGER NOT NULL UNIQUE CHECK (revision > 0),
            spec JSONB NOT NULL,
            reason TEXT NOT NULL
        );
        CREATE INDEX schedule_configuration_created ON schedule_configurations (created_at);
        CREATE TRIGGER immutable_archive BEFORE UPDATE OR DELETE ON schedule_configurations
            FOR EACH ROW EXECUTE FUNCTION reject_archive_mutation();
        CREATE TABLE scheduler_runtime (
            id VARCHAR PRIMARY KEY,
            revision INTEGER NOT NULL CHECK (revision >= 0),
            observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
            owner_token VARCHAR(36),
            effective_enabled BOOLEAN NOT NULL,
            status VARCHAR NOT NULL,
            details JSONB NOT NULL
        );
        CREATE INDEX promotion_history_order ON model_promotions (experiment_id, created_at, id);
    """)


def downgrade():
    raise RuntimeError("Restore a verified backup instead of deleting schedule configuration history")
