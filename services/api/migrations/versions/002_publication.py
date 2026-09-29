"""Enforce exact experiment-domain membership at atomic publication."""

from alembic import op

revision = "002"
down_revision = "001"


def upgrade():
    op.execute("""
    CREATE FUNCTION validate_publication() RETURNS trigger AS $$
    DECLARE expected integer; actual integer; invalid integer;
    BEGIN
      SELECT count(*) INTO expected FROM grid_cells c JOIN experiment_versions e ON c.grid_id=e.grid_id WHERE e.id=NEW.experiment_id;
      SELECT count(*) INTO actual FROM forecast_cells WHERE forecast_id=NEW.id;
      SELECT count(*) INTO invalid FROM forecast_cells f WHERE f.forecast_id=NEW.id AND NOT EXISTS
        (SELECT 1 FROM grid_cells c JOIN experiment_versions e ON c.grid_id=e.grid_id WHERE e.id=NEW.experiment_id AND c.id=f.cell_id);
      IF actual != expected OR expected=0 OR invalid>0 THEN RAISE EXCEPTION 'Publication requires exactly the retained experiment domain'; END IF;
      IF NEW.mode='prospective' AND NOT (NEW.input_cutoff <= NEW.issued_at AND NEW.issued_at < NEW.start) THEN
        RAISE EXCEPTION 'Invalid prospective timing';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER complete_publication BEFORE INSERT ON forecast_runs FOR EACH ROW EXECUTE FUNCTION validate_publication();
    """)


def downgrade():
    raise RuntimeError("Restore a verified backup to a new database instead of downgrading research history")
