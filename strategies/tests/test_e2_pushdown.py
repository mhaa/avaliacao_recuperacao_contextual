from __future__ import annotations

from core.contract import Candidate, Request
from storage.base import GET_CANDIDATES_FILTERED
from storage.tests.fakes import FakeStorageAdapter
from strategies.e2_pushdown import E2Pushdown


def _candidate(item_id, score, context_ids=()):
    return Candidate(item_id=item_id, score=score, context_ids=frozenset(context_ids))


def test_required_primitives_is_get_candidates_filtered_only():
    assert E2Pushdown.required_primitives == frozenset({GET_CANDIDATES_FILTERED})


async def test_only_calls_get_candidates_filtered_primitive():
    storage = FakeStorageAdapter(candidates_by_user={1: [_candidate(1, 1.0, [1])]})
    req = Request(user_id=1, context=[1], exclude=[], k=20)
    await E2Pushdown().retrieve(storage, req)
    assert {call[0] for call in storage.calls} == {GET_CANDIDATES_FILTERED}


async def test_returns_only_items_matching_context():
    storage = FakeStorageAdapter(
        candidates_by_user={
            1: [
                _candidate(10, 5.0, [1]),
                _candidate(20, 4.0, [2]),
                _candidate(30, 3.0, [1, 2]),
            ]
        }
    )
    req = Request(user_id=1, context=[1], exclude=[], k=20)
    response = await E2Pushdown().retrieve(storage, req)
    assert {item.item_id for item in response.items} == {10, 30}


async def test_applies_exclusion_after_pushdown_filter():
    storage = FakeStorageAdapter(
        candidates_by_user={1: [_candidate(10, 5.0, [1]), _candidate(20, 4.0, [1])]}
    )
    req = Request(user_id=1, context=[1], exclude=[10], k=20)
    response = await E2Pushdown().retrieve(storage, req)
    assert [item.item_id for item in response.items] == [20]


async def test_orders_and_ranks_results():
    storage = FakeStorageAdapter(
        candidates_by_user={
            1: [_candidate(1, 1.0, [1]), _candidate(2, 3.0, [1]), _candidate(3, 2.0, [1])]
        }
    )
    req = Request(user_id=1, context=[1], exclude=[], k=20)
    response = await E2Pushdown().retrieve(storage, req)
    assert [(item.item_id, item.rank) for item in response.items] == [
        (2, 1),
        (3, 2),
        (1, 3),
    ]
