from __future__ import annotations

import pytest

from core.contract import Candidate, Request
from storage.base import GET_CANDIDATES, LOAD_ITEM_CONTEXTS
from storage.tests.fakes import FakeStorageAdapter, prepared
from strategies.e1_app_filter import E1AppFilter


def _candidate(item_id, score):
    return Candidate(item_id=item_id, score=score)


def test_required_primitives_are_get_candidates_and_the_catalog_load():
    """A pertença item->contexto virou dado de catálogo carregado uma vez na
    montagem (core/catalog.py), então E-1 exige a primitiva de carga além da
    leitura em massa — ver CONTEXTO.md, "Catálogo item->contexto residente na
    aplicação"."""
    assert E1AppFilter.required_primitives == frozenset({GET_CANDIDATES, LOAD_ITEM_CONTEXTS})


async def test_filters_by_context_in_app():
    storage = FakeStorageAdapter(
        candidates_by_user={1: [_candidate(10, 5.0), _candidate(20, 4.0), _candidate(30, 3.0)]},
        item_contexts={10: frozenset({1}), 20: frozenset({2}), 30: frozenset({1, 2})},
    )
    strategy = await prepared(E1AppFilter(), storage)
    req = Request(user_id=1, context=[1], exclude=[], k=20)
    response = await strategy.retrieve(storage, req)
    assert {item.item_id for item in response.items} == {10, 30}


async def test_context_predicate_is_and_across_requested_contexts():
    storage = FakeStorageAdapter(
        candidates_by_user={1: [_candidate(10, 5.0), _candidate(20, 4.0)]},
        item_contexts={10: frozenset({1}), 20: frozenset({1, 2})},
    )
    strategy = await prepared(E1AppFilter(), storage)
    req = Request(user_id=1, context=[1, 2], exclude=[], k=20)
    response = await strategy.retrieve(storage, req)
    assert [item.item_id for item in response.items] == [20]


async def test_item_absent_from_catalog_never_matches_a_context():
    """Item sem entrada no catálogo tem pertença vazia — com predicado não
    vazio, nunca passa. Guarda contra o catálogo silenciosamente incompleto
    virar 'tudo elegível'."""
    storage = FakeStorageAdapter(
        candidates_by_user={1: [_candidate(10, 5.0)]},
        item_contexts={},
    )
    strategy = await prepared(E1AppFilter(), storage)
    req = Request(user_id=1, context=[1], exclude=[], k=20)
    response = await strategy.retrieve(storage, req)
    assert response.items == []


async def test_applies_exclusion_after_filtering():
    storage = FakeStorageAdapter(
        candidates_by_user={1: [_candidate(10, 5.0), _candidate(20, 4.0)]}
    )
    strategy = await prepared(E1AppFilter(), storage)
    req = Request(user_id=1, context=[], exclude=[10], k=20)
    response = await strategy.retrieve(storage, req)
    assert [item.item_id for item in response.items] == [20]


async def test_orders_and_ranks_results():
    storage = FakeStorageAdapter(
        candidates_by_user={1: [_candidate(1, 1.0), _candidate(2, 3.0), _candidate(3, 2.0)]}
    )
    strategy = await prepared(E1AppFilter(), storage)
    req = Request(user_id=1, context=[], exclude=[], k=20)
    response = await strategy.retrieve(storage, req)
    assert [(item.item_id, item.rank) for item in response.items] == [
        (2, 1),
        (3, 2),
        (1, 3),
    ]


async def test_request_path_only_calls_get_candidates():
    """O catálogo é lido na montagem, nunca por requisição — esta é a guarda
    de regressão contra o N+1 que existia antes (500 SMEMBERS no Valkey, 500
    consultas CQL no ScyllaDB, por requisição)."""
    storage = FakeStorageAdapter(
        candidates_by_user={1: [_candidate(1, 1.0)]},
        item_contexts={1: frozenset({7})},
    )
    strategy = await prepared(E1AppFilter(), storage)
    req = Request(user_id=1, context=[7], exclude=[], k=20)
    await strategy.retrieve(storage, req)
    assert {call[0] for call in storage.calls} == {GET_CANDIDATES}


async def test_catalog_is_loaded_once_at_assembly_not_per_request():
    storage = FakeStorageAdapter(
        candidates_by_user={1: [_candidate(1, 1.0)]},
        item_contexts={1: frozenset({7})},
    )
    strategy = E1AppFilter()
    await strategy.prepare(storage)
    req = Request(user_id=1, context=[7], exclude=[], k=20)
    await strategy.retrieve(storage, req)
    await strategy.retrieve(storage, req)
    assert [call[0] for call in storage.calls].count(LOAD_ITEM_CONTEXTS) == 1


async def test_retrieve_without_prepare_fails_loudly():
    """Sem a carga de montagem, E-1 não tem como avaliar o predicado. Falhar
    aqui é melhor que devolver silenciosamente resposta vazia."""
    storage = FakeStorageAdapter(candidates_by_user={1: [_candidate(1, 1.0)]})
    req = Request(user_id=1, context=[7], exclude=[], k=20)
    with pytest.raises(RuntimeError, match="prepare"):
        await E1AppFilter().retrieve(storage, req)


async def test_unknown_user_returns_empty_response_not_error():
    storage = FakeStorageAdapter()
    strategy = await prepared(E1AppFilter(), storage)
    req = Request(user_id=999, context=[], exclude=[], k=20)
    response = await strategy.retrieve(storage, req)
    assert response.items == []
    assert response.returned_count == 0
