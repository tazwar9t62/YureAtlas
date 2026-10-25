"""Exercise test-run ownership and cleanup without a PostgreSQL connection."""

import os
import select
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import UUID

import pytest

from scripts import test_backend as harness

DATABASE_URL = "postgresql+psycopg://researcher@127.0.0.1:55432/yureatlas"
TEST_NAME = "yureatlas_test_" + "1" * 32
TEST_TOKEN = "2" * 32
OWNER = f"{TEST_NAME}:{TEST_TOKEN}"


@pytest.fixture
def owned_run(tmp_path, monkeypatch):
    state = SimpleNamespace(
        artifact=tmp_path / "owned-artifacts",
        errors={},
        commands=[],
        children=[],
        disposed=[],
        actual_database=TEST_NAME,
        actual_owner=OWNER,
        database_oid=12345,
        cleanup_oid=12345,
        database_comment=None,
        child_callback=None,
    )

    class Engine:
        def __init__(self, database):
            self.database = database

        def connect(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, parameters=None):
            sql = str(statement)
            operation = sql.split()[0].lower()
            state.commands.append((self.database, operation, sql))
            if parameters:
                assert parameters == {"name": TEST_NAME}
            if operation in state.errors:
                raise state.errors[operation]
            if sql.startswith("SELECT oid FROM") and "identity" in state.errors:
                raise state.errors["identity"]
            if operation == "comment":
                state.database_comment = OWNER
            return SimpleNamespace(
                one=lambda: (state.actual_database, state.actual_owner),
                scalar_one=lambda: state.database_oid,
                one_or_none=lambda: (state.cleanup_oid, state.database_comment),
            )

        def dispose(self):
            state.disposed.append(self.database)

    def make_artifacts(*, prefix):
        assert prefix.startswith(TEST_NAME)
        state.artifact.mkdir()
        return str(state.artifact)

    def child(args, env, timeout):
        state.children.append((args, env.copy(), timeout))
        assert state.artifact.is_dir()
        assert (state.artifact / harness.OWNER_FILE).read_text() == OWNER
        if state.child_callback:
            state.child_callback(args, env)
        if args[2] in state.errors:
            raise state.errors[args[2]]

    identifiers = iter([UUID("1" * 32), UUID(TEST_TOKEN)])
    monkeypatch.setattr(harness, "uuid4", lambda: next(identifiers))
    monkeypatch.setattr(harness, "database_engine", lambda url: Engine(url.database))
    monkeypatch.setattr(harness.tempfile, "mkdtemp", make_artifacts)
    monkeypatch.setattr(harness, "run_child", child)
    return state


def dropped_databases(state):
    return [sql for _database, operation, sql in state.commands if operation == "drop"]


@pytest.mark.parametrize("stage", ["create", "comment", "alembic", "pytest"])
def test_setup_and_child_failures_clean_only_created_resources(owned_run, stage):
    error = (
        subprocess.CalledProcessError(7, [sys.executable, "-m", stage])
        if stage in {"alembic", "pytest"}
        else RuntimeError(f"{stage} failed")
    )
    owned_run.errors[stage] = error

    with pytest.raises(type(error)) as raised:
        harness.run_tests(DATABASE_URL)

    assert raised.value is error
    assert len(dropped_databases(owned_run)) == (stage != "create")
    assert not owned_run.artifact.exists()
    assert "postgres" in owned_run.disposed
    assert [args[2] for args, _env, _timeout in owned_run.children] == {
        "create": [],
        "comment": [],
        "alembic": ["alembic"],
        "pytest": ["alembic", "pytest"],
    }[stage]


def test_success_uses_owned_resources_and_preserves_research_artifacts(owned_run, tmp_path, monkeypatch):
    research = tmp_path / "research-artifacts"
    research.mkdir()
    sentinel = research / "keep.json"
    sentinel.write_text("research evidence")
    monkeypatch.setenv("ARTIFACT_DIR", str(research))

    harness.run_tests(DATABASE_URL)

    assert sentinel.read_text() == "research evidence"
    assert os.environ["ARTIFACT_DIR"] == str(research)
    assert not owned_run.artifact.exists()
    assert dropped_databases(owned_run) == [f'DROP DATABASE "{TEST_NAME}" WITH (FORCE)']
    for args, env, timeout in owned_run.children:
        assert args[:2] == [sys.executable, "-m"]
        assert env["ARTIFACT_DIR"] == str(owned_run.artifact)
        assert env["YUREATLAS_TEST_DATABASE"] == TEST_NAME
        assert env["YUREATLAS_TEST_TOKEN"] == TEST_TOKEN
        assert env["SCHEDULE_ENABLED"] == "false"
        assert timeout > 0


def test_migration_mode_uses_the_same_owned_lifecycle_without_preupgrading_head(owned_run):
    harness.run_tests(DATABASE_URL, migration_check=True)

    assert len(owned_run.children) == 1
    args, env, timeout = owned_run.children[0]
    assert args == [sys.executable, "-m", "scripts.check_migrations"]
    assert env["YUREATLAS_TEST_DATABASE"] == TEST_NAME
    assert env["YUREATLAS_TEST_TOKEN"] == TEST_TOKEN
    assert env["ARTIFACT_DIR"] == str(owned_run.artifact)
    assert timeout == 300
    assert dropped_databases(owned_run) == [f'DROP DATABASE "{TEST_NAME}" WITH (FORCE)']
    assert not owned_run.artifact.exists()


def test_migration_checker_failure_survives_failed_drop_and_cleans_artifacts(owned_run, capsys):
    error = subprocess.CalledProcessError(11, [sys.executable, "-m", "scripts.check_migrations"])
    owned_run.errors.update(
        {"scripts.check_migrations": error, "drop": RuntimeError("database disconnected")}
    )
    with pytest.raises(subprocess.CalledProcessError) as raised:
        harness.run_tests(DATABASE_URL, migration_check=True)
    assert raised.value is error
    assert not owned_run.artifact.exists()
    assert "postgres" in owned_run.disposed
    assert f"Database {TEST_NAME} may remain" in capsys.readouterr().err


def test_migration_mode_checks_database_ownership_before_starting_checker(owned_run):
    owned_run.actual_owner = "foreign"
    with pytest.raises(ValueError, match="Actual integration database"):
        harness.run_tests(DATABASE_URL, migration_check=True)
    assert owned_run.children == []
    assert len(dropped_databases(owned_run)) == 1
    assert not owned_run.artifact.exists()


def test_drop_failure_preserves_child_error_and_still_removes_artifacts(owned_run, capsys):
    error = subprocess.CalledProcessError(9, [sys.executable, "-m", "pytest"])
    owned_run.errors.update(pytest=error, drop=RuntimeError("database disconnected"))

    with pytest.raises(subprocess.CalledProcessError) as raised:
        harness.run_tests(DATABASE_URL)

    assert raised.value is error
    assert not owned_run.artifact.exists()
    assert "postgres" in owned_run.disposed
    assert f"Database {TEST_NAME} may remain" in capsys.readouterr().err


def test_successful_tests_with_failed_drop_are_not_reported_as_success(owned_run, capsys):
    owned_run.errors["drop"] = RuntimeError("database disconnected")

    with pytest.raises(RuntimeError, match="Integration cleanup failed"):
        harness.run_tests(DATABASE_URL)

    assert not owned_run.artifact.exists()
    assert TEST_NAME in capsys.readouterr().err


@pytest.mark.parametrize("changed", ["oid", "comment"])
def test_replaced_database_is_preserved_while_owned_artifacts_are_removed(owned_run, capsys, changed):
    def replace_database(args, _env):
        if args[2] == "pytest":
            if changed == "oid":
                owned_run.cleanup_oid += 1
            else:
                owned_run.database_comment = "another run"

    owned_run.child_callback = replace_database
    with pytest.raises(RuntimeError, match="Integration cleanup failed"):
        harness.run_tests(DATABASE_URL)

    assert dropped_databases(owned_run) == []
    assert not owned_run.artifact.exists()
    assert f"Database {TEST_NAME} may remain" in capsys.readouterr().err


def test_unconfirmed_created_database_identity_is_not_dropped(owned_run, capsys):
    error = RuntimeError("identity query disconnected")
    owned_run.errors["identity"] = error

    with pytest.raises(RuntimeError) as raised:
        harness.run_tests(DATABASE_URL)

    assert raised.value is error
    assert dropped_databases(owned_run) == []
    assert not owned_run.artifact.exists()
    assert f"Database {TEST_NAME} may remain" in capsys.readouterr().err


def test_artifact_cleanup_failure_preserves_original_error_and_drops_database(owned_run, monkeypatch, capsys):
    error = subprocess.CalledProcessError(9, [sys.executable, "-m", "pytest"])
    owned_run.errors["pytest"] = error

    def cannot_remove(_path):
        raise PermissionError("artifact directory unavailable")

    monkeypatch.setattr(harness.shutil, "rmtree", cannot_remove)
    with pytest.raises(subprocess.CalledProcessError) as raised:
        harness.run_tests(DATABASE_URL)

    assert raised.value is error
    assert len(dropped_databases(owned_run)) == 1
    assert owned_run.artifact.is_dir()
    assert str(owned_run.artifact) in capsys.readouterr().err


def test_keyboard_interrupt_cleans_database_and_artifacts(owned_run):
    interruption = KeyboardInterrupt()
    owned_run.errors["pytest"] = interruption

    with pytest.raises(KeyboardInterrupt) as raised:
        harness.run_tests(DATABASE_URL)

    assert raised.value is interruption
    assert len(dropped_databases(owned_run)) == 1
    assert not owned_run.artifact.exists()


def test_changed_artifact_owner_is_preserved_and_reported(owned_run, capsys):
    def change_owner(args, _env):
        if args[2] == "pytest":
            (owned_run.artifact / harness.OWNER_FILE).write_text("another run")
            (owned_run.artifact / "keep.json").write_text("foreign evidence")

    owned_run.child_callback = change_owner
    with pytest.raises(RuntimeError, match="Integration cleanup failed"):
        harness.run_tests(DATABASE_URL)

    assert (owned_run.artifact / "keep.json").read_text() == "foreign evidence"
    assert len(dropped_databases(owned_run)) == 1
    assert "Artifact directory" in capsys.readouterr().err


def test_replaced_artifact_directory_is_preserved_even_with_copied_marker(owned_run, capsys):
    def replace_directory(args, _env):
        if args[2] == "pytest":
            owned_run.artifact.rename(owned_run.artifact.with_name("original-artifacts"))
            owned_run.artifact.mkdir()
            (owned_run.artifact / harness.OWNER_FILE).write_text(OWNER)
            (owned_run.artifact / "keep.json").write_text("replacement evidence")

    owned_run.child_callback = replace_directory
    with pytest.raises(RuntimeError, match="Integration cleanup failed"):
        harness.run_tests(DATABASE_URL)

    assert (owned_run.artifact / "keep.json").read_text() == "replacement evidence"
    assert "Artifact directory" in capsys.readouterr().err


def isolation_env(directory):
    directory.mkdir()
    (directory / harness.OWNER_FILE).write_text(OWNER)
    return {
        "YUREATLAS_INTEGRATION": "1",
        "YUREATLAS_TEST_DATABASE": TEST_NAME,
        "YUREATLAS_TEST_TOKEN": TEST_TOKEN,
        "DATABASE_URL": DATABASE_URL.rsplit("/", 1)[0] + "/" + TEST_NAME,
        "ARTIFACT_DIR": str(directory),
    }


@pytest.mark.parametrize("override", ["dbname=yureatlas", "database=yureatlas", "service=research"])
def test_url_database_overrides_are_rejected_before_resource_creation(owned_run, override):
    with pytest.raises(ValueError, match="query overrides"):
        harness.run_tests(DATABASE_URL + "?" + override)

    assert owned_run.commands == []
    assert not owned_run.artifact.exists()


@pytest.mark.parametrize("field,value", [("actual_database", "yureatlas"), ("actual_owner", "foreign")])
def test_actual_database_identity_is_checked_before_migration(owned_run, field, value):
    setattr(owned_run, field, value)

    with pytest.raises(ValueError, match="Actual integration database"):
        harness.run_tests(DATABASE_URL)

    assert owned_run.children == []
    assert len(dropped_databases(owned_run)) == 1
    assert not owned_run.artifact.exists()
    assert TEST_NAME in owned_run.disposed


@pytest.mark.parametrize(
    "foreign_path", ["wrong_owner", "missing_owner", "directory_symlink", "marker_symlink"]
)
def test_foreign_artifacts_are_rejected_before_database_access(tmp_path, monkeypatch, foreign_path):
    artifact = tmp_path / "artifacts"
    env = isolation_env(artifact)
    marker = artifact / harness.OWNER_FILE
    if foreign_path == "wrong_owner":
        marker.write_text("foreign")
    elif foreign_path == "missing_owner":
        marker.unlink()
    elif foreign_path == "directory_symlink":
        link = tmp_path / "alias"
        link.symlink_to(artifact, target_is_directory=True)
        env["ARTIFACT_DIR"] = str(link)
    else:
        original = tmp_path / "foreign-marker"
        marker.rename(original)
        marker.symlink_to(original)

    def forbidden_database(_url):
        pytest.fail("Foreign artifacts must be rejected before database access")

    monkeypatch.setattr(harness, "database_engine", forbidden_database)
    with pytest.raises((ValueError, FileNotFoundError)):
        harness.validate_isolation(env)
    assert artifact.is_dir()


def test_import_has_no_database_process_or_filesystem_side_effects(tmp_path):
    source = """
import runpy
import signal
import subprocess
import sys
import tempfile
import sqlalchemy
def forbidden(*args, **kwargs):
    raise AssertionError('Import attempted a resource mutation')
sqlalchemy.create_engine = forbidden
subprocess.Popen = forbidden
tempfile.mkdtemp = forbidden
signal.signal = forbidden
runpy.run_path(sys.argv[1], run_name='import_safety_probe')
print('import completed')
"""
    result = subprocess.run(
        [sys.executable, "-c", source, str(harness.ROOT / "scripts/test_backend.py")],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "import completed"
    assert list(tmp_path.iterdir()) == []


def test_migration_checker_import_is_safe_and_direct_unsafe_execution_refuses_writes(tmp_path):
    source = """
import runpy
import subprocess
import sys
from pathlib import Path
import sqlalchemy
def forbidden(*args, **kwargs):
    raise AssertionError('RESOURCE_MUTATION_ATTEMPTED')
sqlalchemy.create_engine = forbidden
subprocess.run = forbidden
Path.write_bytes = forbidden
runpy.run_module('scripts.check_migrations', run_name='import_safety_probe')
print('import completed', flush=True)
runpy.run_module('scripts.check_migrations', run_name='__main__')
"""
    env = dict(
        os.environ, PYTHONPATH=str(harness.ROOT), PYTHONDONTWRITEBYTECODE="1", DATABASE_URL=DATABASE_URL
    )
    for key in ("YUREATLAS_TEST_DATABASE", "YUREATLAS_TEST_TOKEN", "ARTIFACT_DIR"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout.strip() == "import completed"
    assert "Integration tests must be launched through scripts/test_backend.py" in result.stderr
    assert "RESOURCE_MUTATION_ATTEMPTED" not in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_direct_unsafe_pytest_is_refused_before_database_access():
    source = """
import pytest
import sqlalchemy
def forbidden(*args, **kwargs):
    raise AssertionError('DATABASE_ACCESS_ATTEMPTED')
sqlalchemy.create_engine = forbidden
raise SystemExit(pytest.main(['--collect-only', '-q', 'services/api/tests/test_persistence.py']))
"""
    env = dict(os.environ, YUREATLAS_INTEGRATION="1", DATABASE_URL=DATABASE_URL)
    for key in ("YUREATLAS_TEST_DATABASE", "YUREATLAS_TEST_TOKEN", "ARTIFACT_DIR"):
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=harness.ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, output
    assert "Unsafe integration environment" in output
    assert "DATABASE_ACCESS_ATTEMPTED" not in output
    assert "AssertionError" not in output


@pytest.mark.parametrize("interrupt", ["timeout", "keyboard", "terminate"])
def test_run_child_reaps_process_before_propagating_interruption(monkeypatch, interrupt):
    args = [sys.executable, "-c", "pass"]
    error = {
        "timeout": subprocess.TimeoutExpired(args, 0.1),
        "keyboard": KeyboardInterrupt(),
        "terminate": SystemExit(143),
    }[interrupt]
    waits = []
    signals = []
    outcomes = iter([error, 0, 0])

    def wait(timeout=None):
        waits.append(timeout)
        result = next(outcomes)
        if isinstance(result, BaseException):
            raise result
        return result

    process = SimpleNamespace(pid=123456, wait=wait)

    def launch(command, **kwargs):
        assert command == args
        assert kwargs == {"cwd": harness.ROOT, "env": {"TEST": "1"}, "start_new_session": True}
        return process

    monkeypatch.setattr(harness.subprocess, "Popen", launch)
    monkeypatch.setattr(harness.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    with pytest.raises(type(error)) as raised:
        harness.run_child(args, {"TEST": "1"}, timeout=0.1)

    assert raised.value is error
    assert waits == [0.1, 5, None]
    assert signals == [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)]


def test_run_child_escalates_and_reaps_unresponsive_process(monkeypatch):
    args = [sys.executable, "-c", "pass"]
    error = subprocess.TimeoutExpired(args, 0.1)
    outcomes = iter([error, subprocess.TimeoutExpired(args, 5), -signal.SIGKILL])
    waits = []
    signals = []

    def wait(timeout=None):
        waits.append(timeout)
        result = next(outcomes)
        if isinstance(result, BaseException):
            raise result
        return result

    process = SimpleNamespace(pid=123456, wait=wait)
    monkeypatch.setattr(harness.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(harness.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        harness.run_child(args, {}, timeout=0.1)

    assert raised.value is error
    assert waits == [0.1, 5, None]
    assert signals == [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)]


@pytest.mark.parametrize("completion", ["timeout", "keyboard", "success"])
def test_run_child_kills_term_ignoring_descendants_after_leader_exits(tmp_path, monkeypatch, completion):
    """An inherited pipe stays open until both the leader and its grandchild exit."""
    ready = tmp_path / "grandchild-ready"
    descendant_source = """
import os
import signal
import sys
import time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()))
time.sleep(30)
"""
    leader_source = """
import subprocess
import sys
import time
from pathlib import Path
subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])
deadline = time.monotonic() + 5
while not Path(sys.argv[2]).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError('Grandchild did not become ready')
    time.sleep(0.01)
if sys.argv[3] != 'success':
    time.sleep(30)
"""
    args = [sys.executable, "-c", leader_source, descendant_source, str(ready), completion]
    original_popen = subprocess.Popen
    launched = []

    def launch(command, **kwargs):
        process = original_popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
        launched.append(process)
        deadline = time.monotonic() + 5
        while not ready.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Real descendant did not complete its readiness handshake")
            time.sleep(0.01)
        if completion == "keyboard":
            original_wait = process.wait
            interrupted = False

            def interrupt_once(timeout=None):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt()
                return original_wait(timeout=timeout)

            monkeypatch.setattr(process, "wait", interrupt_once)
        return process

    monkeypatch.setattr(harness.subprocess, "Popen", launch)
    cleanup_confirmed = False
    try:
        if completion == "success":
            harness.run_child(args, dict(os.environ), timeout=2)
        else:
            expected = KeyboardInterrupt if completion == "keyboard" else subprocess.TimeoutExpired
            with pytest.raises(expected):
                harness.run_child(args, dict(os.environ), timeout=0.05)
        process = launched[0]
        assert process.poll() is not None, "The leader must be reaped before run_child returns"
        readable, _, _ = select.select([process.stdout], [], [], 3)
        assert readable, "A surviving descendant still holds the stdout pipe open"
        assert os.read(process.stdout.fileno(), 4096) == b"", "Child output indicates a fixture failure"
        cleanup_confirmed = True
    finally:
        # Also clean up if the regression returns: these are only processes created by this test.
        for process in launched:
            if not cleanup_confirmed:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=3)
            process.stdout.close()
