"""Testa load/upload_results.py:upload_directory sem GCS real — um cliente
fake grava (blob_name -> conteúdo) num dict, e o teste confere os nomes de
blob e a contagem, não uma rede de verdade (mesmo padrão de
infra/scripts/tests: sem credencial/rede real nos testes unitários)."""

from __future__ import annotations

from pathlib import Path

from load.upload_results import upload_directory


class _FakeBlob:
    def __init__(self, store: dict[str, bytes], name: str):
        self._store = store
        self._name = name

    def upload_from_filename(self, path: str) -> None:
        self._store[self._name] = Path(path).read_bytes()


class _FakeBucket:
    def __init__(self, store: dict[str, bytes]):
        self._store = store

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self._store, name)


class _FakeClient:
    def __init__(self, store: dict[str, bytes]):
        self._store = store

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(self._store)


def test_upload_directory_uploads_every_file_with_prefixed_relative_path(tmp_path, monkeypatch):
    (tmp_path / "rep0").mkdir()
    (tmp_path / "rep0" / "k6-raw.json").write_text("raw")
    (tmp_path / "rep0" / "manifest.json").write_text("manifest")
    (tmp_path / "saturation.json").write_text("saturation")

    store: dict[str, bytes] = {}
    monkeypatch.setattr("load.upload_results.storage.Client", lambda: _FakeClient(store))

    count = upload_directory(tmp_path, "some-bucket", "e1-postgres/triagem/20260101T000000Z")

    assert count == 3
    assert set(store) == {
        "e1-postgres/triagem/20260101T000000Z/rep0/k6-raw.json",
        "e1-postgres/triagem/20260101T000000Z/rep0/manifest.json",
        "e1-postgres/triagem/20260101T000000Z/saturation.json",
    }
    assert store["e1-postgres/triagem/20260101T000000Z/saturation.json"] == b"saturation"


def test_upload_directory_skips_subdirectory_entries_themselves(tmp_path, monkeypatch):
    (tmp_path / "empty_subdir").mkdir()
    (tmp_path / "file.txt").write_text("x")

    store: dict[str, bytes] = {}
    monkeypatch.setattr("load.upload_results.storage.Client", lambda: _FakeClient(store))

    count = upload_directory(tmp_path, "some-bucket", "prefix")

    assert count == 1
    assert set(store) == {"prefix/file.txt"}
