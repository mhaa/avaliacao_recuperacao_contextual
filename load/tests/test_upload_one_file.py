"""Testa load/upload_one_file.py:upload_one_file sem GCS real — mesmo padrão
de load/tests/test_upload_results.py (cliente fake, sem rede/credencial de
verdade nos testes unitários)."""

from __future__ import annotations

from pathlib import Path

from load.upload_one_file import upload_one_file


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


def test_upload_one_file_uploads_to_the_given_blob_name(tmp_path, monkeypatch):
    local_path = tmp_path / "k6-raw.json"
    local_path.write_text("raw")

    store: dict[str, bytes] = {}
    monkeypatch.setattr("load.upload_one_file.storage.Client", lambda: _FakeClient(store))

    upload_one_file(local_path, "some-bucket", "e1-postgres/triagem/20260101T000000Z/rep0/k6-raw.json")

    assert store == {"e1-postgres/triagem/20260101T000000Z/rep0/k6-raw.json": b"raw"}
