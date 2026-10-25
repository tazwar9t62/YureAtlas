"""Run integration checks using resources owned by this invocation only."""

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
NAME_PATTERN = re.compile(r"yureatlas_test_[0-9a-f]{32}\Z")
OWNER_FILE = ".yureatlas-test-owner"
CONNECT_ARGS = {"connect_timeout": 5, "options": "-c statement_timeout=30000 -c lock_timeout=10000"}


def checked_url(value):
    url = make_url(value)
    if url.get_backend_name() != "postgresql" or {"dbname", "database", "service"} & url.query.keys():
        raise ValueError("Use a PostgreSQL URL without database/service query overrides")
    return url


def database_engine(url):
    return create_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool, connect_args=CONNECT_ARGS)


def validate_isolation(env):
    """Refuse execution unless the actual database and artifacts carry this run's token."""
    name = env.get("YUREATLAS_TEST_DATABASE", "")
    token = env.get("YUREATLAS_TEST_TOKEN", "")
    if not NAME_PATTERN.fullmatch(name) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("Integration tests must be launched through scripts/test_backend.py")
    url = checked_url(env.get("DATABASE_URL", ""))
    if url.database != name:
        raise ValueError("Integration database does not match the owned test database")
    artifact = Path(env.get("ARTIFACT_DIR", ""))
    owner = f"{name}:{token}"
    marker = artifact / OWNER_FILE
    if (
        not artifact.is_absolute()
        or artifact.is_symlink()
        or marker.is_symlink()
        or marker.read_text() != owner
    ):
        raise ValueError("Integration artifact directory does not match the owned test directory")
    engine = database_engine(url)
    try:
        with engine.connect() as connection:
            actual_name, actual_owner = connection.execute(
                text(
                    "SELECT current_database(), shobj_description(oid, 'pg_database') "
                    "FROM pg_database WHERE datname = current_database()"
                )
            ).one()
        if (actual_name, actual_owner) != (name, owner):
            raise ValueError("Actual integration database or ownership token does not match")
    finally:
        engine.dispose()


def run_child(args, env, timeout):
    """Reap the child process group before resource cleanup, including on interruption."""
    process = subprocess.Popen(args, cwd=ROOT, env=env, start_new_session=True)
    try:
        code = process.wait(timeout=timeout)
        if code:
            raise subprocess.CalledProcessError(code, args)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass  # The finally block kills the entire group, even if its leader exited.
        raise
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_tests(database_url, *, migration_check=False):
    url = checked_url(database_url)
    name = "yureatlas_test_" + uuid4().hex
    token = uuid4().hex
    owner = f"{name}:{token}"
    admin = database_engine(url.set(database="postgres"))
    created = False
    database_oid = None
    marked = False
    artifact = None
    identity = None
    print(f"Disposable database: {name}", flush=True)
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
            database_oid = connection.execute(
                text(
                    "SELECT oid FROM pg_database WHERE datname = :name AND datdba = "
                    "(SELECT oid FROM pg_roles WHERE rolname = current_user)"
                ),
                {"name": name},
            ).scalar_one()
            # Identifier and token contain only a fixed prefix and UUID hex.
            connection.execute(text(f"COMMENT ON DATABASE \"{name}\" IS '{owner}'"))
            marked = True
        artifact = Path(tempfile.mkdtemp(prefix=f"{name}_")).absolute()
        if artifact.is_symlink():
            raise ValueError("Artifact directory must not be a symlink")
        stat = artifact.lstat()
        identity = (stat.st_dev, stat.st_ino)
        (artifact / OWNER_FILE).write_text(owner)
        env = dict(
            os.environ,
            DATABASE_URL=url.set(database=name).render_as_string(hide_password=False),
            ARTIFACT_DIR=str(artifact),
            YUREATLAS_INTEGRATION="1",
            YUREATLAS_TEST_DATABASE=name,
            YUREATLAS_TEST_TOKEN=token,
            SCHEDULE_ENABLED="false",
            OMP_NUM_THREADS="2",
            OPENBLAS_NUM_THREADS="2",
        )
        print(f"Disposable artifacts: {artifact}", flush=True)
        validate_isolation(env)  # Verify the effective connection before Alembic can write.
        if migration_check:
            run_child([sys.executable, "-m", "scripts.check_migrations"], env, timeout=300)
        else:
            run_child([sys.executable, "-m", "alembic", "upgrade", "head"], env, timeout=120)
            run_child([sys.executable, "-m", "pytest", "-q", "-m", "integration"], env, timeout=900)
    finally:
        primary_failure = sys.exc_info()[0] is not None
        failures = []
        if not created:
            print(
                f"Database creation was not confirmed; no database removed. Inspect {name}.", file=sys.stderr
            )
        if created:
            try:
                with admin.connect() as connection:
                    actual = connection.execute(
                        text(
                            "SELECT oid, shobj_description(oid, 'pg_database') "
                            "FROM pg_database WHERE datname = :name AND datdba = "
                            "(SELECT oid FROM pg_roles WHERE rolname = current_user)"
                        ),
                        {"name": name},
                    ).one_or_none()
                    if database_oid is None or actual != (database_oid, owner if marked else None):
                        raise ValueError("Database identity/ownership changed; refusing removal")
                    connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
            except Exception as error:  # noqa: BLE001 - attempt all cleanup without masking the test error
                failures.append(f"Database {name} may remain ({type(error).__name__})")
        if artifact is not None:
            try:
                stat = artifact.lstat()
                if artifact.is_symlink() or (stat.st_dev, stat.st_ino) != identity:
                    raise ValueError("Artifact directory identity changed; refusing removal")
                marker = artifact / OWNER_FILE
                if marker.exists() and (marker.is_symlink() or marker.read_text() != owner):
                    raise ValueError("Artifact ownership changed; refusing removal")
                shutil.rmtree(artifact)
            except Exception as error:  # noqa: BLE001 - one cleanup failure must not prevent the next
                failures.append(f"Artifact directory {artifact} may remain ({type(error).__name__})")
        try:
            admin.dispose()
        except Exception as error:  # noqa: BLE001 - preserve the original failure while reporting cleanup
            failures.append(f"Admin connection disposal failed ({type(error).__name__})")
        for failure in failures:
            print(f"Cleanup failed: {failure}", file=sys.stderr)
        if failures and not primary_failure:
            raise RuntimeError("Integration cleanup failed; see reported resource names")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-check", action="store_true", help="Check a populated schema002 upgrade")
    args = parser.parse_args()
    from yureatlas.config import Settings

    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminate)
    run_tests(Settings(_env_file=ROOT / ".env").database_url, migration_check=args.migration_check)


if __name__ == "__main__":
    main()
