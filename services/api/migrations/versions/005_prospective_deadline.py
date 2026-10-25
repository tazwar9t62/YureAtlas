"""Check new prospective publications at deferred transaction finalization."""

from alembic import op

revision = "005"
down_revision = "004"


def upgrade():
    # Keep the existing exact-grid/timestamp checks and immutable historical rows.
    # clock_timestamp advances during a transaction; CURRENT_TIMESTAMP does not.
    # This guards transaction validation, not the later physical WAL/visibility time.
    op.execute("""
        CREATE FUNCTION validate_prospective_deadline() RETURNS trigger AS $$
        BEGIN
          IF NEW.mode='prospective' AND clock_timestamp() >= NEW.start THEN
            RAISE EXCEPTION USING
              ERRCODE = '23514',
              CONSTRAINT = 'prospective_publication_deadline',
              MESSAGE = 'Missed publication deadline during transaction finalization';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE CONSTRAINT TRIGGER prospective_publication_deadline
          AFTER INSERT ON forecast_runs
          DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION validate_prospective_deadline();
    """)


def downgrade():
    raise RuntimeError("Restore a verified backup instead of weakening publication deadline enforcement")
