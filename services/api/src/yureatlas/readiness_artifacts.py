"""Bounded artifact-root access check; the child imports only the standard library.

This file is also executed directly with ``python -I -S``. It must not import
application configuration, database drivers, or model artifacts.
"""

import math
import os
import secrets
import stat
import subprocess
import sys
from time import monotonic

PROBE_PREFIX = ".yureatlas-readiness-"
MAX_OUTPUT_BYTES = 80
MESSAGES = {
    "artifact_probe_ok": "Artifact storage passed an owned temporary write, sync, read and cleanup check.",
    "artifact_root_unavailable": "Artifact storage could not be opened or created as a directory.",
    "artifact_probe_write_failed": "Artifact storage could not create, write or sync its temporary probe.",
    "artifact_probe_read_failed": "Artifact storage could not read back the expected temporary probe contents.",
    "artifact_probe_cleanup_failed": "Artifact probe cleanup failed; an owned temporary file may remain.",
    "artifact_probe_replaced": "The temporary probe changed identity or contents; the replacement was preserved.",
    "artifact_probe_timeout": "Artifact probing timed out; its process may still be terminating and an owned temporary file may remain.",
    "artifact_probe_process_failed": "The isolated artifact probe could not complete or return a valid result.",
}


def _identity(info):
    return info.st_dev, info.st_ino


def _read_bounded(descriptor, limit):
    result = bytearray()
    while len(result) < limit:
        chunk = os.read(descriptor, limit - len(result))
        if not chunk:
            break
        result.extend(chunk)
    return bytes(result)


def _cleanup_probe(directory, name, identity, expected):
    """Unlink only the created inode and its full or partially written token.

    The original descriptor remains open, preventing inode reuse while this
    check runs. A held directory descriptor also survives root-path replacement.
    A mismatching path or token is left untouched, including symlinks.
    """
    descriptor = None
    try:
        before = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if _identity(before) != identity or not stat.S_ISREG(before.st_mode):
            return "artifact_probe_replaced"
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        if _identity(os.fstat(descriptor)) != identity:
            return "artifact_probe_replaced"
        contents = _read_bounded(descriptor, len(expected) + 1)
        # A failed write can leave an empty/partial owned file. Neither a
        # different token nor extra bytes establish ownership for cleanup.
        if len(contents) > len(expected) or contents != expected[: len(contents)]:
            return "artifact_probe_replaced"
        latest = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if _identity(latest) != identity or not stat.S_ISREG(latest.st_mode):
            return "artifact_probe_replaced"
        os.unlink(name, dir_fd=directory)
        return None
    except OSError:
        return "artifact_probe_cleanup_failed"
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass  # The child process exit closes any remaining descriptor.


def _probe_once(root):
    """Run only inside the isolated child (or controlled temporary-root tests)."""
    directory = descriptor = None
    directory_identity = None
    identity = None
    name = PROBE_PREFIX + secrets.token_hex(24)
    expected = b"YureAtlas artifact readiness\n" + secrets.token_bytes(32)
    code = "artifact_root_unavailable"
    created = False
    try:
        os.makedirs(root, exist_ok=True)
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        directory_identity = _identity(os.fstat(directory))
        code = "artifact_probe_write_failed"
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        created = True
        identity = _identity(os.fstat(descriptor))
        offset = 0
        while offset < len(expected):
            written = os.write(descriptor, expected[offset:])
            if written <= 0:
                raise OSError("Incomplete probe write")
            offset += written
        os.fsync(descriptor)
        code = "artifact_probe_read_failed"
        read_descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
        read_code = code
        try:
            if _identity(os.fstat(read_descriptor)) != identity:
                read_code = "artifact_probe_replaced"
            elif _read_bounded(read_descriptor, len(expected) + 1) == expected:
                read_code = "artifact_probe_ok"
        finally:
            os.close(read_descriptor)
        code = read_code
    except OSError:
        pass  # Only the fixed stage code crosses the process boundary.
    except Exception:  # noqa: BLE001 - unexpected child failures must not expose paths or exceptions
        code = "artifact_probe_process_failed"
    finally:
        if created:
            cleanup = (
                _cleanup_probe(directory, name, identity, expected)
                if identity is not None
                else "artifact_probe_cleanup_failed"
            )
            if cleanup is not None:
                code = cleanup
        if code == "artifact_probe_ok":
            try:
                current_root = os.stat(root)
                if _identity(current_root) != directory_identity or not stat.S_ISDIR(current_root.st_mode):
                    code = "artifact_root_unavailable"
            except OSError:
                code = "artifact_root_unavailable"
        for opened in (descriptor, directory):
            if opened is not None:
                try:
                    os.close(opened)
                except OSError:
                    if code == "artifact_probe_ok":
                        code = "artifact_probe_cleanup_failed"
    return code


def _child_command(root):
    return [sys.executable, "-I", "-S", os.path.abspath(__file__), "--child", os.fspath(root)]


def _result(code):
    return {
        "status": "ok" if code == "artifact_probe_ok" else "failed",
        "code": code,
        "message": MESSAGES[code],
    }


def _terminate(process, deadline):
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=max(0, deadline - monotonic()))
    except (subprocess.TimeoutExpired, OSError):
        pass  # A blocked OS operation must not turn reaping into an unlimited wait.


def probe_artifact_root(path, *, timeout=3.0):
    """Inspect storage without letting a blocked filesystem stall readiness.

    The timeout includes a reserved kill/reap interval. An uninterruptible OS
    filesystem operation can delay actual process death; never wait forever for
    it or attempt unsafe parent-side deletion after a timeout.
    """
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 3
    ):
        raise ValueError("Artifact probe timeout must be a finite number in (0, 3] seconds")
    process = None
    termination_attempted = False
    deadline = monotonic() + timeout
    grace = min(0.25, timeout / 4)
    try:
        process = subprocess.Popen(
            _child_command(path), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        try:
            process.wait(timeout=max(0, deadline - monotonic() - grace))
        except subprocess.TimeoutExpired:
            termination_attempted = True
            _terminate(process, deadline)
            return _result("artifact_probe_timeout")
        if process.returncode != 0 or process.stdout is None:
            return _result("artifact_probe_process_failed")
        # Never accumulate arbitrary child output or wait for an inherited
        # pipe. A faulty, overproducing child instead hits the process timeout.
        os.set_blocking(process.stdout.fileno(), False)
        output = process.stdout.read(MAX_OUTPUT_BYTES + 1)
        code = output.decode("ascii").strip() if output is not None else ""
        if not output or len(output) > MAX_OUTPUT_BYTES or code not in MESSAGES:
            return _result("artifact_probe_process_failed")
        return _result(code)
    except (OSError, ValueError, UnicodeError):
        return _result("artifact_probe_process_failed")
    finally:
        if process is not None:
            if process.returncode is None and not termination_attempted:
                _terminate(process, deadline)
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass  # The child is already terminated or boundedly terminating.


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--child":
        raise SystemExit(2)
    print(_probe_once(sys.argv[2]))
