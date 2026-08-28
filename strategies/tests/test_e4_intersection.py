from __future__ import annotations

from core.contract import Candidate, Request
from storage.base import INTERSECT
from storage.tests.fakes import FakeStorageAdapter
from strategies.e4_intersection import E4Intersection


def _candidate(item_id, score):
    return Candidate(item_id=item_id, score=score)


def test_required_primitives_is_intersect_only():
    assert E4Intersection.required_primitives == frozenset({INTERSECT})


async def test_single_context_intersects_user_candidates_with_inverted_list():
    storage = FakeStorageAdapter(
        candidates_by_user={1: [_candidate(10, 9.0), _candidate(20, 8.0), _candidate(30, 7.0)]},
        inverted_lists={5: frozenset({10, 30})},
    )
    req = Request(user_id=1, context=[5], exclude=[], k=20)
    response = await E4Intersection().retrieve(storage, req)
    assert {item.item_id for item in response.items} == {10, 30}


async def test_composed_context_is_and_across_inverted_lists():
    """Ao contrário de E-3, as listas invertidas não são truncadas — a
    interseção de múltiplos context_ids é correta mesmo em contexto
    composto (ver docstring de strategies/e4_intersection.py)."""
    storage = FakeStorageAdapter(
        candidates_by_user={1: [_candidate(10, 9.0), _candidate(20, 8.0)]},
        inverted_lists={5: frozenset({10, 20}), 8: frozenset({20})},
    )
    req = Request(user_id=1, context=[5, 8], exclude=[], k=20)
    response = await E4Intersection().retrieve(storage, req)
    assert [item.item_id for item in response.items] == [20]


async def test_exclusion_never_reaches_intersect_primitive_call():
    """Mesma guarda de regressão de E-3: exclusão nunca vira parâmetro da
    interseção, é aplicada depois, na aplicação."""
    storage = FakeStorageAdapter(
        candidates_by_user={1: [_candidate(10, 9.0), _candidate(20, 8.0)]},
        inverted_lists={5: frozenset({10, 20})},
    )
    req = Request(user_id=1, context=[5], exclude=[10], k=20)
    response = await E4Intersection().retrieve(storage, req)

    ((primitive, call_args),) = storage.calls
    assert primitive == INTERSECT
    assert call_args == (1, (5,))  # sem exclude na chamada

    assert [item.item_id for item in response.items] == [20]


async def test_orders_and_ranks_results():
    storage = FakeStorageAdapter(
        candidates_by_user={1: [_candidate(1, 1.0), _candidate(2, 3.0), _candidate(3, 2.0)]},
        inverted_lists={5: frozenset({1, 2, 3})},
    )
    req = Request(user_id=1, context=[5], exclude=[], k=20)
    response = await E4Intersection().retrieve(storage, req)
    assert [(item.item_id, item.rank) for item in response.items] == [
        (2, 1),
        (3, 2),
        (1, 3),
    ]
