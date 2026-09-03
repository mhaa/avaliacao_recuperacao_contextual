from __future__ import annotations

import json

from fastapi.testclient import TestClient

from core.contract import RESPONSE_BYTE_BUDGET, Candidate
from service.http_app import create_app
from storage.tests.fakes import FakeStorageAdapter
from strategies.e1_app_filter import E1AppFilter


def _candidate(item_id, score):
    return Candidate(item_id=item_id, score=score)


def test_recommendations_endpoint_returns_exact_contract_shape():
    storage = FakeStorageAdapter(
        candidates_by_user={1: [_candidate(1, 3.0), _candidate(2, 1.0)]}
    )
    app = create_app(E1AppFilter(), storage)
    # `with`, não TestClient(app) solto: só o context manager dispara o
    # lifespan, que é onde a carga de montagem da célula acontece
    # (service/http_app.py:_lifespan).
    with TestClient(app) as client:
        resp = client.post(
            "/v1/recommendations", json={"user_id": 1, "context": [], "exclude": [], "k": 20}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"items", "returned_count"}
    assert set(body["items"][0].keys()) == {"item_id", "score", "rank"}
    assert body["returned_count"] == 2
    assert [item["item_id"] for item in body["items"]] == [1, 2]


def test_recommendations_endpoint_rejects_unknown_request_field():
    storage = FakeStorageAdapter(candidates_by_user={1: [_candidate(1, 1.0)]})
    app = create_app(E1AppFilter(), storage)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/recommendations",
            json={"user_id": 1, "context": [], "exclude": [], "k": 20, "extra_field": True},
        )

    assert resp.status_code == 422


def test_k20_response_payload_within_byte_budget():
    candidates = [_candidate(i, 1.0 - i * 0.001) for i in range(20)]
    storage = FakeStorageAdapter(candidates_by_user={1: candidates})
    app = create_app(E1AppFilter(), storage)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/recommendations", json={"user_id": 1, "context": [], "exclude": [], "k": 20}
        )

    payload_size = len(json.dumps(resp.json()).encode("utf-8"))
    assert payload_size <= RESPONSE_BYTE_BUDGET + 200  # HTTP/JSON tem overhead sobre o núcleo
