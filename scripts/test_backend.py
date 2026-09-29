"""Create and remove a dedicated disposable PostgreSQL database for integration tests."""

import os
import subprocess
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from yureatlas.config import settings

name = "yureatlas_test_" + uuid4().hex[:12]
base_url = make_url(settings.database_url)
admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
with admin.connect() as connection:
    connection.execute(text(f'CREATE DATABASE "{name}"'))
env = dict(
    os.environ,
    DATABASE_URL=base_url.set(database=name).render_as_string(hide_password=False),
    YUREATLAS_INTEGRATION="1",
    ARTIFACT_DIR=f"data/tests/{name}",
    OMP_NUM_THREADS="2",
    OPENBLAS_NUM_THREADS="2",
)
try:
    subprocess.run([".venv/bin/alembic", "upgrade", "head"], env=env, check=True)
    subprocess.run([".venv/bin/pytest", "-q"], env=env, check=True)
finally:
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    import shutil

    shutil.rmtree(env["ARTIFACT_DIR"], ignore_errors=True)
