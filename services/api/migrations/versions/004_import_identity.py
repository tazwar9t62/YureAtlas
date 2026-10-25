"""Append immutable local-import identity without adopting legacy catalogue data."""

from alembic import op

revision = "004"
down_revision = "003"


def upgrade():
    op.execute("""
        CREATE TABLE import_manifests (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP WITH TIME ZONE NOT NULL,
            source_id VARCHAR(36) NOT NULL REFERENCES catalogue_sources(id),
            ingestion_id VARCHAR(36) NOT NULL UNIQUE REFERENCES ingestion_runs(id),
            namespace VARCHAR NOT NULL,
            format VARCHAR NOT NULL CHECK (format IN ('jma', 'csv')),
            artifact VARCHAR NOT NULL,
            raw_hash VARCHAR(64) NOT NULL,
            line_count INTEGER NOT NULL CHECK (line_count >= 0),
            mapping JSONB NOT NULL,
            parser_version VARCHAR NOT NULL,
            coverage_declared BOOLEAN NOT NULL,
            start TIMESTAMP WITH TIME ZONE NOT NULL,
            "end" TIMESTAMP WITH TIME ZONE NOT NULL CHECK ("end" > start),
            request_digest VARCHAR(64) NOT NULL,
            UNIQUE (source_id, request_digest)
        );
        CREATE INDEX ix_import_manifests_created_at ON import_manifests(created_at);
        CREATE INDEX ix_import_manifests_source_namespace ON import_manifests(source_id, namespace);
        CREATE INDEX ix_import_manifests_source_hash ON import_manifests(source_id, raw_hash);
        CREATE TRIGGER immutable_archive BEFORE UPDATE OR DELETE ON import_manifests
          FOR EACH ROW EXECUTE FUNCTION reject_archive_mutation();
        CREATE TRIGGER immutable_archive BEFORE UPDATE OR DELETE ON ingestion_runs
          FOR EACH ROW EXECUTE FUNCTION reject_archive_mutation();
    """)


def downgrade():
    raise RuntimeError("Restore a verified backup instead of deleting import identity evidence")
