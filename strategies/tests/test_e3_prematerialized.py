from __future__ import annotations

from core.contract import Candidate, Request
from storage.base import GET_CANDIDATES, GET_PREMATERIALIZED
from storage.tests.fakes import FakeStorageAdapter
from strategies.e3_prematerialized import E3Prematerialized


def _candidate(item_id, score, context_ids=()):
    return Candidate(item_id=item_id, score=score, context_ids=frozenset(context_ids))


def test_required_primitives_includes_prematerialized_and_get_candidates():
    assert E3Prematerialized.required_primitives == frozenset(
        {GET_PREMATERIALIZED, GET_CANDIDATES}
    )


async def test_single_context_uses_prematerialized_primitive_only():
    storage = FakeStorageAdapter(
        prematerialized={(1, "5"): [_candidate(10, 9.0), _candidate(20, 8.0)]}
    )
    req = Request(user_id=1, context=[5], exclude=[], k=20)
    response = await E3Prematerialized().retrieve(storage, req)
    assert {call[0] for call in storage.calls} == {GET_PREMATERIALIZED}
    assert [item.item_id for item in response.items] == [10, 20]


async def test_composed_context_falls_back_to_get_candidates_and_filters_in_app():
    """Regressão: interseção de dois top-40 pré-materializados poderia
    perder um item que só está no topo da interseção composta — por isso
    E-3 cai para leitura completa + filtro em app quando há mais de um
    context_id (ver docstring de strategies/e3_prematerialized.py)."""
    storage = FakeStorageAdapter(
        candidates_by_user={
            1: [
                _candidate(10, 5.0, context_ids=[5]),
                _candidate(20, 4.0, context_ids=[5, 8]),
            ]
        }
    )
    req = Request(user_id=1, context=[5, 8], exclude=[], k=20)
    response = await E3Prematerialized().retrieve(storage, req)
    assert {call[0] for call in storage.calls} == {GET_CANDIDATES}
    assert [item.item_id for item in response.items] == [20]


async def test_empty_context_falls_back_to_get_candidates():
    storage = FakeStorageAdapter(candidates_by_user={1: [_candidate(10, 5.0)]})
    req = Request(user_id=1, context=[], exclude=[], k=20)
    response = await E3Prematerialized().retrieve(storage, req)
    assert {call[0] for call in storage.calls} == {GET_CANDIDATES}
    assert [item.item_id for item in response.items] == [10]


async def test_exclusion_never_reaches_prematerialized_primitive_call():
    """Guarda de regressão central da Etapa 4: a exclusão de sessão nunca
    pode virar parâmetro da leitura pré-materializada — ela é aplicada
    depois, na aplicação, sobre o resultado já lido."""
    storage = FakeStorageAdapter(
        prematerialized={(1, "5"): [_candidate(10, 9.0), _candidate(20, 8.0)]}
    )
    req = Request(user_id=1, context=[5], exclude=[10], k=20)
    response = await E3Prematerialized().retrieve(storage, req)

    ((primitive, call_args),) = storage.calls
    assert primitive == GET_PREMATERIALIZED
    assert call_args == (1, "5")  # sem exclude na chamada

    assert [item.item_id for item in response.items] == [20]
