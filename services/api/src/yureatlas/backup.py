"""Streaming artifact archive with path and checksum validation on restore."""

import hashlib
import sys
import tarfile
from pathlib import PurePosixPath

from .artifacts import put, read
from .config import settings


def export_artifacts(stream):
    with tarfile.open(fileobj=stream, mode="w|gz") as archive:
        for path in sorted(settings.artifact_dir.glob("*/*")):
            if path.is_file():
                ref = path.relative_to(settings.artifact_dir).as_posix()
                read(ref)
                archive.add(path, arcname=ref, recursive=False)


def restore_artifacts(stream):
    count = 0
    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        for member in archive:
            ref = PurePosixPath(member.name)
            if (
                not member.isfile()
                or ref.is_absolute()
                or ".." in ref.parts
                or len(ref.parts) != 2
                or member.size > 512 * 1024 * 1024
            ):
                raise ValueError("Unsafe or oversized artifact archive member")
            if ref.suffix not in {".json", ".joblib", ".parquet", ".csv", ".txt"} or len(ref.stem) != 64:
                raise ValueError("Unknown artifact format in backup")
            content = archive.extractfile(member).read()
            if hashlib.sha256(content).hexdigest() != ref.stem:
                raise ValueError("Backup artifact checksum mismatch")
            actual = put(content, ref.suffix[1:])
            if actual != str(ref):
                raise ValueError("Backup artifact path does not match its checksum")
            count += 1
    return count


if __name__ == "__main__":
    if sys.argv[1] == "export":
        export_artifacts(sys.stdout.buffer)
    elif sys.argv[1] == "restore":
        print(f"Restored {restore_artifacts(sys.stdin.buffer)} verified artifacts", file=sys.stderr)
    else:
        raise SystemExit("Use export or restore")
