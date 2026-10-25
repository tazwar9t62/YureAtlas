"""The readiness probe touches only owned temporary files and has a process bound."""

import io
import os
import stat
import subprocess
import sys
from time import monotonic

import pytest
from yureatlas import readiness_artifacts as artifacts


def probe_files(root):
    return list(root.glob(artifacts.PROBE_PREFIX + "*"))


def test_real_child_creates_missing_root_and_preserves_existing_artifacts(tmp_path, monkeypatch):
    root = tmp_path / "new" / "artifact-root"
    # The standalone child must not import application settings/site packages.
    monkeypatch.setenv("DATABASE_URL", "not-an-application-database-url")
    first = artifacts.probe_artifact_root(root)
    assert first["status"] == "ok" and first["code"] == "artifact_probe_ok"
    assert root.is_dir() and list(root.iterdir()) == []
    sentinel = root / "existing-research.joblib"
    sentinel.write_bytes(b"existing immutable artifact; never deserialize")
    sentinel.chmod(0o440)
    original = sentinel.stat()
    second = artifacts.probe_artifact_root(root)
    assert second == first
    assert list(root.iterdir()) == [sentinel]
    current = sentinel.stat()
    assert (current.st_ino, current.st_mode, current.st_mtime_ns) == (
        original.st_ino,
        original.st_mode,
        original.st_mtime_ns,
    )
    assert sentinel.read_bytes() == b"existing immutable artifact; never deserialize"
    assert str(root) not in first["message"]


def test_real_child_rejects_file_as_root_without_mutating_it(tmp_path):
    root = tmp_path / "not-a-directory"
    root.write_bytes(b"must survive")
    result = artifacts.probe_artifact_root(root)
    assert result["status"] == "failed" and result["code"] == "artifact_root_unavailable"
    assert root.read_bytes() == b"must survive" and list(tmp_path.iterdir()) == [root]
    assert str(root) not in str(result)


def test_exclusive_creation_does_not_read_or_remove_a_colliding_existing_file(tmp_path, monkeypatch):
    token = "a" * 48
    existing = tmp_path / (artifacts.PROBE_PREFIX + token)
    existing.write_bytes(b"foreign file with a colliding name")
    monkeypatch.setattr(artifacts.secrets, "token_hex", lambda length: token)
    original = existing.stat()
    assert artifacts._probe_once(tmp_path) == "artifact_probe_write_failed"
    assert existing.read_bytes() == b"foreign file with a colliding name"
    assert existing.stat().st_ino == original.st_ino and list(tmp_path.iterdir()) == [existing]


@pytest.mark.parametrize(
    "failure,code",
    [
        ("create", "artifact_probe_write_failed"),
        ("write", "artifact_probe_write_failed"),
        ("sync", "artifact_probe_write_failed"),
        ("read", "artifact_probe_read_failed"),
        ("mismatch", "artifact_probe_read_failed"),
        ("delete", "artifact_probe_cleanup_failed"),
    ],
)
def test_access_failures_are_sanitized_and_owned_cleanup_is_attempted(tmp_path, monkeypatch, failure, code):
    sentinel = tmp_path / "existing.json"
    sentinel.write_bytes(b"original")
    original_open, original_read, original_unlink = os.open, os.read, os.unlink
    read_failed = False

    def fail(*args, **kwargs):
        raise PermissionError("private path and operating-system detail must not escape")

    def open_probe(name, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            fail()
        return original_open(name, flags, *args, **kwargs)

    def read_probe(descriptor, length):
        nonlocal read_failed
        if not read_failed:
            read_failed = True
            if failure == "read":
                fail()
            return b"incorrect-readback"
        return original_read(descriptor, length)

    def unlink_probe(name, *args, **kwargs):
        if str(name).startswith(artifacts.PROBE_PREFIX):
            fail()
        return original_unlink(name, *args, **kwargs)

    if failure == "create":
        monkeypatch.setattr(artifacts.os, "open", open_probe)
    elif failure == "write":
        monkeypatch.setattr(artifacts.os, "write", fail)
    elif failure == "sync":
        monkeypatch.setattr(artifacts.os, "fsync", fail)
    elif failure in {"read", "mismatch"}:
        monkeypatch.setattr(artifacts.os, "read", read_probe)
    else:
        monkeypatch.setattr(artifacts.os, "unlink", unlink_probe)
    observed = artifacts._probe_once(tmp_path)
    assert observed == code
    assert "private" not in artifacts._result(observed)["message"]
    assert sentinel.read_bytes() == b"original"
    if failure == "delete":
        assert len(probe_files(tmp_path)) == 1
        assert "may remain" in artifacts._result(observed)["message"]
    else:
        assert probe_files(tmp_path) == []


def test_short_io_is_completed_and_probe_permissions_are_private(tmp_path, monkeypatch):
    original_write, original_read, original_sync = os.write, os.read, os.fsync
    calls = {"write": 0, "read": 0}

    def short_write(descriptor, data):
        calls["write"] += 1
        return original_write(descriptor, data[:3])

    def short_read(descriptor, length):
        calls["read"] += 1
        return original_read(descriptor, min(5, length))

    def verify_mode(descriptor):
        assert stat.S_IMODE(os.fstat(descriptor).st_mode) == 0o600
        original_sync(descriptor)

    monkeypatch.setattr(artifacts.os, "write", short_write)
    monkeypatch.setattr(artifacts.os, "read", short_read)
    monkeypatch.setattr(artifacts.os, "fsync", verify_mode)
    assert artifacts._probe_once(tmp_path) == "artifact_probe_ok"
    assert calls["write"] > 1 and calls["read"] > 1 and list(tmp_path.iterdir()) == []


def test_read_descriptor_close_failure_cannot_report_success(tmp_path, monkeypatch):
    original_close = os.close
    first = True

    def close_with_error(descriptor):
        nonlocal first
        original_close(descriptor)
        if first:
            first = False
            raise OSError("Injected close failure")

    monkeypatch.setattr(artifacts.os, "close", close_with_error)
    assert artifacts._probe_once(tmp_path) == "artifact_probe_read_failed"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("replacement", ["file", "symlink", "changed_token"])
def test_foreign_replacement_or_changed_token_is_preserved(tmp_path, monkeypatch, replacement):
    sentinel = tmp_path / "existing-artifact.json"
    sentinel.write_bytes(b"existing artifact")
    original_sync = os.fsync
    replaced = []

    def replace_after_sync(descriptor):
        original_sync(descriptor)
        [path] = probe_files(tmp_path)
        if replacement == "changed_token":
            # Same inode, but ownership can no longer be established by token.
            path.write_bytes(b"foreign contents")
        else:
            path.unlink()
            if replacement == "file":
                path.write_bytes(b"foreign contents")
            else:
                path.symlink_to(sentinel)
        replaced.append(path)

    monkeypatch.setattr(artifacts.os, "fsync", replace_after_sync)
    assert artifacts._probe_once(tmp_path) == "artifact_probe_replaced"
    assert len(replaced) == 1 and replaced[0].exists()
    assert replaced[0].read_bytes() == (
        b"existing artifact" if replacement == "symlink" else b"foreign contents"
    )
    assert sentinel.read_bytes() == b"existing artifact"


def test_replaced_root_cannot_report_success_and_cleanup_stays_in_original_directory(tmp_path, monkeypatch):
    root, old_root = tmp_path / "configured", tmp_path / "renamed"
    root.mkdir()
    original_sync = os.fsync

    def replace_root(descriptor):
        original_sync(descriptor)
        root.rename(old_root)
        root.mkdir()
        (root / "foreign.json").write_bytes(b"replacement directory content")

    monkeypatch.setattr(artifacts.os, "fsync", replace_root)
    assert artifacts._probe_once(root) == "artifact_root_unavailable"
    assert list(old_root.iterdir()) == []
    assert (root / "foreign.json").read_bytes() == b"replacement directory content"


def test_missing_root_after_cleanup_is_not_healthy(tmp_path, monkeypatch):
    root = tmp_path / "removed"
    root.mkdir()
    original_unlink = os.unlink

    def remove_root_after_probe(name, *args, **kwargs):
        original_unlink(name, *args, **kwargs)
        root.rmdir()

    monkeypatch.setattr(artifacts.os, "unlink", remove_root_after_probe)
    assert artifacts._probe_once(root) == "artifact_root_unavailable"
    assert not root.exists()


def capture_process(monkeypatch):
    captured = []
    original = subprocess.Popen

    def launch(*args, **kwargs):
        process = original(*args, **kwargs)
        captured.append(process)
        return process

    monkeypatch.setattr(artifacts.subprocess, "Popen", launch)
    return captured


def test_timeout_kills_and_reaps_real_child_and_honestly_leaves_owned_probe(tmp_path, monkeypatch):
    orphan = tmp_path / (artifacts.PROBE_PREFIX + "timeout-test")
    script = "import os,sys,time; fd=os.open(sys.argv[1], os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600); os.write(fd,b'pending'); os.close(fd); time.sleep(60)"
    monkeypatch.setattr(
        artifacts, "_child_command", lambda root: [sys.executable, "-I", "-S", "-c", script, str(orphan)]
    )
    processes = capture_process(monkeypatch)
    start = monotonic()
    result = artifacts.probe_artifact_root(tmp_path, timeout=1)
    elapsed = monotonic() - start
    assert result["code"] == "artifact_probe_timeout" and result["status"] == "failed"
    assert elapsed < 2 and orphan.read_bytes() == b"pending"
    assert "may remain" in result["message"]
    [process] = processes
    assert process.returncode is not None and process.stdout.closed
    with pytest.raises(ChildProcessError):
        os.waitpid(process.pid, os.WNOHANG)


@pytest.mark.parametrize(
    "script,expected",
    [
        (
            "import sys; print('private path /sensitive/root',file=sys.stderr); sys.exit(1)",
            "artifact_probe_process_failed",
        ),
        ("print('unrecognized response with /sensitive/root')", "artifact_probe_process_failed"),
        ("print('x'*1000)", "artifact_probe_process_failed"),
        ("import sys; sys.stdout.write('x'*10000000); sys.stdout.flush()", "artifact_probe_timeout"),
    ],
)
def test_faulty_child_output_is_bounded_and_never_exposed(tmp_path, monkeypatch, script, expected):
    monkeypatch.setattr(artifacts, "_child_command", lambda root: [sys.executable, "-I", "-S", "-c", script])
    processes = capture_process(monkeypatch)
    result = artifacts.probe_artifact_root(tmp_path, timeout=0.5)
    assert result["code"] == expected and "/sensitive" not in str(result)
    assert processes[0].returncode is not None and processes[0].stdout.closed


def test_delayed_os_termination_cannot_cause_unbounded_reaping(tmp_path, monkeypatch):
    waits, kills = [], []
    output = io.BytesIO()

    class UninterruptibleProcess:
        stdout = output
        returncode = None

        def wait(self, *, timeout):
            waits.append(timeout)
            raise subprocess.TimeoutExpired("isolated probe", timeout)

        def kill(self):
            kills.append(True)

    monkeypatch.setattr(artifacts.subprocess, "Popen", lambda *args, **kwargs: UninterruptibleProcess())
    result = artifacts.probe_artifact_root(tmp_path, timeout=0.1)
    assert result["code"] == "artifact_probe_timeout" and output.closed
    assert kills == [True] and len(waits) == 2 and all(0 <= value <= 0.1 for value in waits)
    assert "still be terminating" in result["message"]


def test_parent_wait_failure_still_kills_and_reaps_real_child(tmp_path, monkeypatch):
    monkeypatch.setattr(
        artifacts,
        "_child_command",
        lambda root: [sys.executable, "-I", "-S", "-c", "import time; time.sleep(60)"],
    )
    original_launch = subprocess.Popen
    processes = []

    def launch(*args, **kwargs):
        process = original_launch(*args, **kwargs)
        original_wait = process.wait
        first = True

        def interrupted_wait(*, timeout):
            nonlocal first
            if first:
                first = False
                raise OSError("Private parent-side wait failure")
            return original_wait(timeout=timeout)

        process.wait = interrupted_wait
        processes.append(process)
        return process

    monkeypatch.setattr(artifacts.subprocess, "Popen", launch)
    result = artifacts.probe_artifact_root(tmp_path, timeout=0.5)
    assert result["code"] == "artifact_probe_process_failed" and "Private" not in str(result)
    [process] = processes
    assert process.returncode is not None and process.stdout.closed
    with pytest.raises(ChildProcessError):
        os.waitpid(process.pid, os.WNOHANG)


def test_process_launch_failure_has_only_safe_fixed_diagnostics(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("private interpreter or filesystem path")

    monkeypatch.setattr(artifacts.subprocess, "Popen", unavailable)
    result = artifacts.probe_artifact_root(tmp_path)
    assert result["code"] == "artifact_probe_process_failed" and "private" not in result["message"]


@pytest.mark.parametrize("timeout", [True, 0, -1, 3.1, float("inf"), float("nan")])
def test_timeout_must_retain_a_finite_process_bound(tmp_path, monkeypatch, timeout):
    monkeypatch.setattr(
        artifacts.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Invalid timeout launched child")
    )
    with pytest.raises(ValueError, match="finite number"):
        artifacts.probe_artifact_root(tmp_path, timeout=timeout)
