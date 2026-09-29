import io
import tarfile

import pytest
from yureatlas.artifacts import put, read
from yureatlas.backup import export_artifacts, restore_artifacts
from yureatlas.config import settings


def test_artifacts_roundtrip_preserves_hash_and_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "artifact_dir", tmp_path / "original")
    ref = put(b'{"forecast":"immutable-provenance"}', "json")
    archive = io.BytesIO()
    export_artifacts(archive)
    monkeypatch.setattr(settings, "artifact_dir", tmp_path / "restored")
    archive.seek(0)
    assert restore_artifacts(archive) == 1
    assert read(ref) == b'{"forecast":"immutable-provenance"}'
    malicious = io.BytesIO()
    with tarfile.open(fileobj=malicious, mode="w:gz") as tar:
        item = tarfile.TarInfo("../../escape.json")
        item.size = 2
        tar.addfile(item, io.BytesIO(b"{}"))
    malicious.seek(0)
    with pytest.raises(ValueError, match="Unsafe"):
        restore_artifacts(malicious)
