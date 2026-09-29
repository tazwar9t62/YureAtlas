"""Consistent Compose database + artifact backups; restore into a NEW installation."""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def call(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def backup():
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    root = Path("backups") / stamp
    root.mkdir(parents=True, exist_ok=False)
    compose = ["docker", "compose"]
    running = json.loads(subprocess.check_output(compose + ["ps", "--format", "json"], text=True))
    resume = [r["Service"] for r in running if r["Service"] in {"api", "worker"} and r["State"] == "running"]
    call(compose + ["stop", "api", "worker"])
    try:
        with (root / "database.dump").open("wb") as stream:
            call(
                compose
                + [
                    "exec",
                    "-T",
                    "db",
                    "pg_dump",
                    "-U",
                    "yureatlas",
                    "-d",
                    "yureatlas",
                    "-Fc",
                    "--no-owner",
                    "--no-acl",
                ],
                stdout=stream,
            )
        with (root / "artifacts.tar.gz").open("wb") as stream:
            call(
                compose
                + ["run", "--rm", "-T", "--no-deps", "api", "python", "-m", "yureatlas.backup", "export"],
                stdout=stream,
            )
        manifest = {
            "format": 1,
            "created_at": stamp,
            "consistency": "API and worker stopped for both snapshots",
            "files": {name: checksum(root / name) for name in ("database.dump", "artifacts.tar.gz")},
        }
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    finally:
        if resume:
            call(compose + ["start"] + resume)
    print(root.resolve())


def restore(directory):
    root = Path(directory).resolve()
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("format") != 1 or set(manifest["files"]) != {"database.dump", "artifacts.tar.gz"}:
        raise SystemExit("Unsupported backup manifest")
    for name, expected in manifest["files"].items():
        if checksum(root / name) != expected:
            raise SystemExit(f"Backup verification failed: {name}")
    project = os.environ.get("RESTORE_PROJECT", "yureatlas-restored")
    if not re.fullmatch(r"yureatlas-restored[-a-z0-9]*", project):
        raise SystemExit(
            "RESTORE_PROJECT must start with yureatlas-restored and contain only lowercase letters, digits and hyphens"
        )
    volumes = subprocess.check_output(["docker", "volume", "ls", "-q"], text=True).splitlines()
    if any(v.startswith(project + "_") for v in volumes):
        raise SystemExit(
            "Restore target already exists; select a NEW RESTORE_PROJECT. Existing research is never overwritten."
        )
    env = dict(
        os.environ,
        WEB_PORT=os.environ.get("RESTORE_WEB_PORT", "3001"),
        API_PORT=os.environ.get("RESTORE_API_PORT", "8001"),
        DB_PORT=os.environ.get("RESTORE_DB_PORT", "55433"),
        SCHEDULE_ENABLED="false",
    )
    env["ALLOWED_ORIGINS"] = f"http://127.0.0.1:{env['WEB_PORT']},http://localhost:{env['WEB_PORT']}"
    compose = ["docker", "compose", "-p", project]
    call(compose + ["up", "-d", "--no-build", "db"], env=env)
    import time

    for _ in range(60):
        ready = subprocess.run(
            compose + ["exec", "-T", "db", "pg_isready", "-U", "yureatlas"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if ready.returncode == 0:
            break
        time.sleep(1)
    with (root / "database.dump").open("rb") as stream:
        call(
            compose
            + [
                "exec",
                "-T",
                "db",
                "pg_restore",
                "-U",
                "yureatlas",
                "-d",
                "yureatlas",
                "--no-owner",
                "--no-acl",
                "--exit-on-error",
            ],
            stdin=stream,
            env=env,
        )
    with (root / "artifacts.tar.gz").open("rb") as stream:
        call(
            compose
            + ["run", "--rm", "-T", "--no-deps", "api", "python", "-m", "yureatlas.backup", "restore"],
            stdin=stream,
            env=env,
        )
    call(compose + ["up", "-d", "--no-build", "api", "worker", "web"], env=env)
    print(
        f"Verified restore ready at http://127.0.0.1:{env['WEB_PORT']} · project {project}. Schedules are disabled until explicitly enabled."
    )


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "backup":
        backup()
    elif len(sys.argv) == 3 and sys.argv[1] == "restore" and sys.argv[2]:
        restore(sys.argv[2])
    else:
        raise SystemExit("Usage: python3 scripts/backup.py backup | restore BACKUP_DIRECTORY")
