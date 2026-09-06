"""`postgres_storage_bytes`/`postgres_table_bytes` exigem um Postgres real;
`scylla_table_bytes`, `opensearch_index_bytes` e `valkey_key_pattern_bytes`
exigem os respectivos bancos reais com o schema aplicado e o fixture do
oráculo carregado — todos marcados `integration` (nunca no default). Aqui
só a escrita do JSON e os coletores contra banco real são testados;
`main()`/o parsing do `STORAGE_RESULT` não têm teste próprio porque só
formata o que os coletores já devolveram (mesmo padrão de
`analysis/probe_report.py`).

    docker compose up -d postgres scylla opensearch valkey
    docker compose run --rm --entrypoint python tools schemas/postgres/apply_schema.py
    docker compose run --rm --entrypoint python tools schemas/postgres/load_oracle_fixture.py
    docker compose run --rm --entrypoint python tools schemas/scylla/apply_schema.py
    docker compose run --rm --entrypoint python tools schemas/scylla/load_oracle_fixture.py
    docker compose run --rm --entrypoint python tools schemas/opensearch/create_index.py
    docker compose run --rm --entrypoint python tools schemas/opensearch/load_oracle_fixture.py
    docker compose run --rm --entrypoint python tools schemas/valkey/load_oracle_fixture.py
    docker compose run --rm tools -m integration analysis/tests/test_storage_size.py -v
"""

from __future__ import annotations

import json
import os

import pytest

from analysis.storage_size import (
    opensearch_index_bytes,
    postgres_table_bytes,
    scylla_table_bytes,
    valkey_key_pattern_bytes,
    write_storage_json,
)


def test_write_storage_json_has_the_expected_shape(tmp_path):
    out = tmp_path / "storage.json"
    write_storage_json(storage_bytes=123456, backend="postgres", path=out)

    payload = json.loads(out.read_text())
    assert payload == {"backend": "postgres", "storage_bytes": 123456}


@pytest.mark.integration
def test_postgres_table_bytes_reflects_the_loaded_oracle_fixture():
    conninfo = os.environ.get("TEST_POSTGRES_DSN", "postgresql://tcc:tcc@postgres:5432/recsys")
    sizes = postgres_table_bytes(conninfo)

    assert set(sizes) == {"candidates", "item_contexts", "prematerialized", "inverted_lists"}
    # candidates/item_contexts/prematerialized sempre existem e têm dado do
    # oráculo carregado; inverted_lists (C=20 linhas, pequena) pode ser 0
    # bytes num Postgres sem VACUUM ainda, mas nunca ausente da resposta.
    assert sizes["candidates"] > 0
    assert sizes["item_contexts"] > 0
    assert sizes["prematerialized"] > 0


@pytest.mark.integration
def test_postgres_table_bytes_returns_zero_for_a_table_that_does_not_exist():
    # Regressão: confirmado ao vivo (nuvem) que passar o nome de uma tabela
    # ausente direto pra pg_total_relation_size(%s) sobe UndefinedTable —
    # Postgres converte o parâmetro pro tipo regclass NO BIND, antes do
    # CASE WHEN proteger nada. postgres_table_bytes precisa nunca fazer
    # isso (ver a CTE em analysis/storage_size.py).
    conninfo = os.environ.get("TEST_POSTGRES_DSN", "postgresql://tcc:tcc@postgres:5432/recsys")
    sizes = postgres_table_bytes(conninfo, tables=["esta_tabela_nao_existe"])

    assert sizes == {"esta_tabela_nao_existe": 0}


@pytest.mark.integration
def test_scylla_table_bytes_returns_all_expected_tables():
    hosts = os.environ.get("TEST_SCYLLA_HOSTS", "scylla").split(",")
    sizes = scylla_table_bytes(hosts)

    assert set(sizes) == {"candidates", "item_contexts", "candidates_by_context", "prematerialized"}
    # system.size_estimates é preenchida por um job periódico do próprio
    # Scylla, não em tempo real após a carga — numa base local pequena e
    # recém-carregada, pode legitimamente estar zerada até o job rodar.
    # O teste garante que a consulta funciona e devolve as 4 tabelas, não
    # que o valor já refletiu a carga (isso é uma limitação documentada da
    # técnica, não um bug do coletor — ver docs/DESIGN.md).
    assert all(isinstance(v, int) and v >= 0 for v in sizes.values())


@pytest.mark.integration
def test_opensearch_index_bytes_reflects_the_loaded_oracle_fixture():
    hosts = [os.environ.get("TEST_OPENSEARCH_HOST", "http://opensearch:9200")]
    sizes = opensearch_index_bytes(hosts)

    assert set(sizes) == {"candidates", "item_contexts"}
    assert sizes["candidates"] > 0
    assert sizes["item_contexts"] > 0


@pytest.mark.integration
def test_valkey_key_pattern_bytes_counts_real_keys_and_samples_memory_usage():
    url = os.environ.get("TEST_VALKEY_URL", "redis://valkey:6379/0")
    sizes = valkey_key_pattern_bytes(url, sample_size=50, seed=0)

    assert set(sizes) == {
        "candidates:*",
        "item_contexts:*",
        "candidates_set:*",
        "inverted:*",
        "prematerialized:*",
    }
    # candidates:*/candidates_set:* têm uma chave por usuário do oráculo
    # (~1000, ver harness/fixtures.py:needed_user_ids) — nunca zero depois
    # de schemas/valkey/load_oracle_fixture.py.
    assert sizes["candidates:*"]["key_count"] > 0
    assert sizes["candidates:*"]["sampled"] == min(50, sizes["candidates:*"]["key_count"])
    assert sizes["candidates:*"]["bytes_estimate"] > 0
