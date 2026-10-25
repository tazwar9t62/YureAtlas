"""Support latest source receipts and automatic model selection without history sorts."""

from alembic import op

revision = "010"
down_revision = "009"


def upgrade():
    op.create_index("ingestion_source_receipt_order", "ingestion_runs", ["source_id", "created_at", "id"])
    op.create_index(
        "model_selection_order",
        "model_versions",
        ["experiment_id", "algorithm", "status", "created_at", "id"],
    )


def downgrade():
    op.drop_index("model_selection_order", table_name="model_versions")
    op.drop_index("ingestion_source_receipt_order", table_name="ingestion_runs")
