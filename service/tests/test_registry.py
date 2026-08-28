from __future__ import annotations

import pytest

from core.config import load_cell_config
from core.registry import (
    STORAGE_REGISTRY,
    STRATEGY_REGISTRY,
    MissingCredential,
    build_storage,
    build_strategy,
)

VIABLE_CELL_IDS = [
    "e1-postgres",
    "e2-postgres",
    "e3-postgres",
    "e4-postgres",
    "e1-valkey",
    "e2-valkey",
    "e3-valkey",
    "e4-valkey",
    "e1-scylla",
    "e2-scylla",
    "e3-scylla",
    "e1-opensearch",
    "e2-opensearch",
    "e4-opensearch",
]


@pytest.mark.parametrize("cell_id", VIABLE_CELL_IDS)
def test_real_cell_strategy_and_storage_resolve(cell_id):
    config = load_cell_config(cell_id)
    assert config.strategy in STRATEGY_REGISTRY
    assert config.storage in STORAGE_REGISTRY


@pytest.mark.parametrize("cell_id", VIABLE_CELL_IDS)
def test_real_cell_builds_a_strategy_instance(cell_id):
    config = load_cell_config(cell_id)
    strategy = build_strategy(config)
    assert strategy.name == config.strategy


def test_build_storage_postgres_raises_missing_credential_without_env(monkeypatch):
    monkeypatch.delenv("POSTGRES_USER", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_DB", raising=False)
    config = load_cell_config("e1-postgres")
    with pytest.raises(MissingCredential) as exc_info:
        build_storage(config)
    assert "POSTGRES_USER" in str(exc_info.value)


def test_build_storage_postgres_builds_conninfo_from_env(monkeypatch):
    monkeypatch.setenv("POSTGRES_USER", "tcc")
    monkeypatch.setenv("POSTGRES_PASSWORD", "tcc")
    monkeypatch.setenv("POSTGRES_DB", "recsys")
    config = load_cell_config("e1-postgres")
    storage = build_storage(config)
    assert storage.name == "postgres"


def test_build_storage_valkey_needs_no_credential():
    config = load_cell_config("e1-valkey")
    storage = build_storage(config)
    assert storage.name == "valkey"


@pytest.mark.integration
def test_build_storage_scylla_needs_no_credential():
    """`ScyllaAdapter.__init__` resolve/conecta no construtor (diferente
    de Postgres/Valkey/OpenSearch, que são preguiçosos) — decisão já
    tomada na Etapa 5 por ser o padrão idiomático do cassandra-driver.
    Por isso este teste, ao contrário dos outros três backends, precisa
    de um host Scylla resolvível/no ar."""
    config = load_cell_config("e1-scylla")
    storage = build_storage(config)
    assert storage.name == "scylla"


def test_build_storage_opensearch_needs_no_credential():
    config = load_cell_config("e1-opensearch")
    storage = build_storage(config)
    assert storage.name == "opensearch"
