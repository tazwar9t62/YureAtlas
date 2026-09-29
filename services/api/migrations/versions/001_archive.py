"""Initial relational archive; frozen SQL, never imported from evolving models."""

from pathlib import Path

from alembic import op

revision = "001"
down_revision = None


def upgrade():
    op.execute(Path(__file__).with_name("001_schema.sql").read_text())
    op.execute("""
        CREATE FUNCTION reject_archive_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'Archive rows are immutable; create a new version'; END;
        $$ LANGUAGE plpgsql;
    """)
    for table in (
        "grid_versions",
        "grid_cells",
        "experiment_versions",
        "earthquake_events",
        "earthquake_revisions",
        "catalogue_snapshots",
        "feature_sets",
        "model_versions",
        "forecast_runs",
        "forecast_cells",
        "evaluation_runs",
        "evaluation_cells",
        "model_promotions",
    ):
        op.execute(
            f"CREATE TRIGGER immutable_archive BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_archive_mutation()"
        )
    # App inserts all children before the run, using a deferred foreign key. Once
    # the parent exists, no further child rows may be inserted by later transactions.
    for child, parent, fk in (
        ("forecast_cells", "forecast_runs", "forecast_id"),
        ("evaluation_cells", "evaluation_runs", "evaluation_id"),
    ):
        op.execute(f"ALTER TABLE {child} ALTER CONSTRAINT {child}_{fk}_fkey DEFERRABLE INITIALLY DEFERRED")
        op.execute(f"""CREATE FUNCTION seal_{child}() RETURNS trigger AS $$
        BEGIN
          IF EXISTS (SELECT 1 FROM {parent} WHERE id = NEW.{fk}) THEN
            RAISE EXCEPTION 'Published archive is sealed';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        CREATE TRIGGER sealed_archive BEFORE INSERT ON {child}
          FOR EACH ROW EXECUTE FUNCTION seal_{child}();""")


def downgrade():
    raise RuntimeError("Archive migration is irreversible. Restore a verified backup into a new database.")
