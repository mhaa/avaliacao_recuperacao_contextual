"""postgres_storage_bytes exige um Postgres real — fica para um teste
`integration` quando fizer sentido; aqui só a escrita do JSON é testada."""

from __future__ import annotations

import json

from analysis.storage_size import write_storage_json


def test_write_storage_json_has_the_expected_shape(tmp_path):
    out = tmp_path / "storage.json"
    write_storage_json(storage_bytes=123456, backend="postgres", path=out)

    payload = json.loads(out.read_text())
    assert payload == {"backend": "postgres", "storage_bytes": 123456}
