from __future__ import annotations

from core.contract import Candidate, Request
from storage.base import GET_CANDIDATES
from storage.tests.fakes import FakeStorageAdapter
from strategies.e1_app_filter import E1AppFilter


def _candidate(item_id, score, context_ids=()):
    return Candidate(item_id=item_id, score=score, context_ids=frozenset(context_ids))


def test_required_primitives_is_get_candidates_only():
    assert E1AppFilter.required_primitives == frozenset({GET_CANDIDATES})


async def test_filters_by_context_in_app():
    storage = FakeStorageAdapter(
        candidates_by_user={
            1: [
                _candidate(10, 5.0, context_ids=[1]),
                _candidate(20, 4.0, context_ids=[2]),
                _candidate(30, 3.0, context_ids=[1, 2]),
            ]
        }
    )
    req = Request(user_id=1, context=[1], exclude=[], k=20)
    response = await E1AppFilter().retrieve(storage, req)
    assert {item.item_id for item in response.items} == {10, 30}


async def test_context_predicate_is_and_across_requested_contexts():
    storage = FakeStorageAdapter(
        candidates_by_user={
            1: [
                _candidate(10, 5.0, context_ids=[1]),
                _candidate(20, 4.0, context_ids=[1, 2]),
            ]
        }
    )
    req = Request(user_id=1, context=[1, 2], exclude=[], k=20)
    response = await E1AppFilter().retrieve(storage, req)
    assert [item.item_id for item in response.items] == [20]


async def test_applies_exclusion_after_filtering():
    storage = FakeStorageAdapter(
        candidates_by_user={1: [_candidate(10, 5.0), _candidate(20, 4.0)]}
    )
    req = Request(user_id=1, context=[], exclude=[10], k=20)
    response = await E1AppFilter().retrieve(storage, req)
    assert [item.item_id for item in response.items] == [20]


async def test_orders_and_ranks_results():
    storage = FakeStorageAdapter(
        candidates_by_user={1: [_candidate(1, 1.0), _candidate(2, 3.0), _candidate(3, 2.0)]}
    )
    req = Request(user_id=1, context=[], exclude=[], k=20)
    response = await E1AppFilter().retrieve(storage, req)
    assert [(item.item_id, item.rank) for item in response.items] == [
        (2, 1),
        (3, 2),
        (1, 3),
    ]


async def test_only_calls_get_candidates_primitive():
    storage = FakeStorageAdapter(candidates_by_user={1: [_candidate(1, 1.0)]})
    req = Request(user_id=1, context=[], exclude=[], k=20)
    await E1AppFilter().retrieve(storage, req)
    assert {call[0] for call in storage.calls} == {GET_CANDIDATES}


async def test_unknown_user_returns_empty_response_not_error():
    storage = FakeStorageAdapter()
    req = Request(user_id=999, context=[], exclude=[], k=20)
    response = await E1AppFilter().retrieve(storage, req)
    assert response.items == []
    assert response.returned_count == 0
