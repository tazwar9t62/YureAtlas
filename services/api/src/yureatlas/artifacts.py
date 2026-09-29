import hashlib
import json
import os
import tempfile
from pathlib import Path

from .config import settings


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False).encode()


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def put(data: bytes, suffix: str) -> str:
    if suffix not in {"json", "joblib", "parquet", "csv", "txt"}:
        raise ValueError("Unsupported artifact format")
    checksum = hashlib.sha256(data).hexdigest()
    ref = f"{checksum[:2]}/{checksum}.{suffix}"
    dest = settings.artifact_dir / ref
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        fd, tmp = tempfile.mkstemp(dir=dest.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    read(ref)  # Existing artifacts must also be verified.
    return ref


def read(ref: str) -> bytes:
    root = settings.artifact_dir.resolve()
    path = (root / ref).resolve()
    if not path.is_relative_to(root) or len(Path(ref).stem) != 64:
        raise ValueError("Invalid artifact reference")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != path.stem:
        raise ValueError("Artifact checksum mismatch")
    return data
