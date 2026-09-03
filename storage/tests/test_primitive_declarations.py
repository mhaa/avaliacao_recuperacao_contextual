"""Cada adaptador declara, sem precisar de conexão real, quais primitivas
suporta — checagem estática usada na montagem da célula (ver
strategies/base.py:check_compatibility).
"""

from __future__ import annotations

from storage.base import (
    ALL_PRIMITIVES,
    GET_CANDIDATES,
    GET_CANDIDATES_FILTERED,
    GET_PREMATERIALIZED,
    INTERSECT,
    LOAD_ITEM_CONTEXTS,
)
from storage.opensearch import OpenSearchAdapter
from storage.postgres import PostgresAdapter
from storage.scylla import ScyllaAdapter
from storage.valkey import ValkeyAdapter


def test_postgres_supports_every_primitive():
    """Todas viáveis por CONTEXTO.md; nenhuma conexão é aberta aqui."""
    assert PostgresAdapter.supported_primitives == ALL_PRIMITIVES


def test_valkey_supports_every_primitive():
    assert ValkeyAdapter.supported_primitives == ALL_PRIMITIVES


def test_scylla_does_not_support_intersect():
    """E-4 é inviável em Scylla por CONTEXTO.md (sem primitiva de
    interseção) — checagem puramente estática, nenhuma conexão é aberta
    (nem precisa: `supported_primitives` é atributo de classe)."""
    assert INTERSECT not in ScyllaAdapter.supported_primitives
    assert ScyllaAdapter.supported_primitives == frozenset(
        {GET_CANDIDATES, GET_CANDIDATES_FILTERED, GET_PREMATERIALIZED, LOAD_ITEM_CONTEXTS}
    )


def test_opensearch_does_not_support_prematerialized():
    """E-3 não tem sentido arquitetural em OpenSearch por CONTEXTO.md —
    checagem puramente estática, nenhuma conexão é aberta."""
    assert GET_PREMATERIALIZED not in OpenSearchAdapter.supported_primitives
    assert OpenSearchAdapter.supported_primitives == frozenset(
        {GET_CANDIDATES, GET_CANDIDATES_FILTERED, INTERSECT, LOAD_ITEM_CONTEXTS}
    )


def test_all_primitives_constant_matches_the_named_constants():
    assert ALL_PRIMITIVES == frozenset(
        {
            GET_CANDIDATES,
            GET_CANDIDATES_FILTERED,
            GET_PREMATERIALIZED,
            INTERSECT,
            LOAD_ITEM_CONTEXTS,
        }
    )
