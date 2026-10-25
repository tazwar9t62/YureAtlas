"""Preserve forecast request targets and one-time resolved execution identities."""

from alembic import op

revision = "008"
down_revision = "007"


def upgrade():
    op.execute("""
        ALTER TABLE jobs ADD COLUMN execution JSONB;
        ALTER TABLE jobs ADD CONSTRAINT forecast_execution_shape CHECK (
            execution IS NULL OR (
                kind = 'forecast' AND jsonb_typeof(execution) = 'object'
                AND execution->>'state' IS NOT NULL
                AND execution->>'state' IN ('target_pinned', 'resolved')
            )
        );
        CREATE FUNCTION preserve_forecast_execution() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.execution IS NOT NULL AND NEW.execution IS DISTINCT FROM OLD.execution THEN
                IF NEW.execution IS NULL
                   OR OLD.execution->>'state' <> 'target_pinned'
                   OR NEW.execution->>'state' IS DISTINCT FROM 'resolved'
                   OR NOT ((NEW.execution - 'state') @> (OLD.execution - 'state')) THEN
                    RAISE EXCEPTION 'Forecast execution identity is immutable once pinned/resolved'
                        USING ERRCODE = '23514', CONSTRAINT = 'forecast_execution_immutable';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER forecast_execution_immutable BEFORE UPDATE OF execution ON jobs
            FOR EACH ROW EXECUTE FUNCTION preserve_forecast_execution();
    """)


def downgrade():
    raise RuntimeError("Restore a verified backup instead of deleting forecast execution identities")
