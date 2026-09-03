from __future__ import annotations

from core.contract import Candidate
from service.grpc_app import RecommendationServicer
from service.proto import recommendation_pb2
from storage.tests.fakes import FakeStorageAdapter, prepared
from strategies.e1_app_filter import E1AppFilter


def _candidate(item_id, score):
    return Candidate(item_id=item_id, score=score)


async def test_retrieve_returns_exact_contract_shape():
    storage = FakeStorageAdapter(
        candidates_by_user={1: [_candidate(1, 3.0), _candidate(2, 1.0)]}
    )
    servicer = RecommendationServicer(await prepared(E1AppFilter(), storage), storage)
    request = recommendation_pb2.Request(user_id=1, context=[], exclude=[], k=20)

    response = await servicer.Retrieve(request, grpc_context=None)

    assert response.returned_count == 2
    assert [item.item_id for item in response.items] == [1, 2]
    assert [item.rank for item in response.items] == [1, 2]


async def test_retrieve_applies_context_and_exclude():
    storage = FakeStorageAdapter(
        candidates_by_user={
            1: [_candidate(1, 3.0), _candidate(2, 2.0), _candidate(3, 1.0)]
        },
        item_contexts={1: frozenset({5}), 2: frozenset({5}), 3: frozenset({9})},
    )
    servicer = RecommendationServicer(await prepared(E1AppFilter(), storage), storage)
    request = recommendation_pb2.Request(user_id=1, context=[5], exclude=[1], k=20)

    response = await servicer.Retrieve(request, grpc_context=None)

    assert [item.item_id for item in response.items] == [2]
