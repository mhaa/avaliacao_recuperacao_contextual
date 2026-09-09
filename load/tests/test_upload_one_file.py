"""Testa load/upload_one_file.py:upload_one_file sem GCS real — mesmo padrão
de load/tests/test_upload_results.py (cliente fake, sem rede/credencial de
verdade nos testes unitários)."""

from __future__ import annotations

from pathlib import Path

from load.upload_one_file import upload_one_file


class _FakeBlob:
    def __init__(self, store: dict[str, bytes], name: str, calls: list[dict]):
        self._store = store
        self._name = name
        self._calls = calls

    def upload_from_filename(self, path: str, **kwargs) -> None:
        self._store[self._name] = Path(path).read_bytes()
        self._calls.append(kwargs)


class _FakeBucket:
    def __init__(self, store: dict[str, bytes], calls: list[dict]):
        self._store = store
        self._calls = calls

    def blob(self, name: str) -> _FakeBlob:
        return _FakeBlob(self._store, name, self._calls)


class _FakeClient:
    def __init__(self, store: dict[str, bytes], calls: list[dict]):
        self._store = store
        self._calls = calls

    def bucket(self, name: str) -> _FakeBucket:
        return _FakeBucket(self._store, self._calls)


def test_upload_one_file_uploads_to_the_given_blob_name(tmp_path, monkeypatch):
    local_path = tmp_path / "k6-raw.json"
    local_path.write_text("raw")

    store: dict[str, bytes] = {}
    calls: list[dict] = []
    monkeypatch.setattr("load.upload_one_file.storage.Client", lambda: _FakeClient(store, calls))

    upload_one_file(local_path, "some-bucket", "e1-postgres/triagem/20260101T000000Z/rep0/k6-raw.json")

    assert store == {"e1-postgres/triagem/20260101T000000Z/rep0/k6-raw.json": b"raw"}


def test_upload_one_file_requires_the_object_to_not_already_exist(tmp_path, monkeypatch):
    # A service account da loadgen só tem roles/storage.objectCreator (não
    # .delete) — sem if_generation_match=0, o cliente GCS assume que pode
    # estar sobrescrevendo um objeto existente e o upload volta 403.
    # Confirmado ao vivo derrubando e3-postgres na confirmação.
    local_path = tmp_path / "k6-raw.json"
    local_path.write_text("raw")

    store: dict[str, bytes] = {}
    calls: list[dict] = []
    monkeypatch.setattr("load.upload_one_file.storage.Client", lambda: _FakeClient(store, calls))

    upload_one_file(local_path, "some-bucket", "e1-postgres/triagem/20260101T000000Z/rep0/k6-raw.json")

    assert calls == [{"if_generation_match": 0}]
