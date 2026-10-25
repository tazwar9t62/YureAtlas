from alembic import context
from yureatlas import tables  # noqa: F401
from yureatlas.db import Base, engine

with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
