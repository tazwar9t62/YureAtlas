from alembic import context
from yureatlas.db import Base, engine
from yureatlas import tables  # noqa: F401

with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
