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


# --- /v1/baseline: o piso da bancada (docs/BENCHMARKS.md, secao 6) ---------------


def _baseline_client(storage=None):
    app = create_app(E1AppFilter(), storage or FakeStorageAdapter())
    return TestClient(app)


def test_baseline_returns_the_same_contract_shape():
    with _baseline_client() as client:
        resp = client.post(
            "/v1/baseline", json={"user_id": 1, "context": [], "exclude": [], "k": 20}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"items", "returned_count"}
    assert set(body["items"][0].keys()) == {"item_id", "score", "rank"}
    assert body["returned_count"] == 20
    assert [item["rank"] for item in body["items"]] == list(range(1, 21))


def test_baseline_never_touches_storage():
    """A razao de existir do endpoint. Se ele encostar no banco, o numero
    medido deixa de ser piso e a subtracao vira ficcao."""
    storage = FakeStorageAdapter(candidates_by_user={1: [_candidate(1, 1.0)]})
    with _baseline_client(storage) as client:
        # Zera DEPOIS do lifespan: `prepare` chama load_item_contexts uma vez
        # na montagem, o que e esperado e nao e o caminho de requisicao.
        storage.calls.clear()
        client.post(
            "/v1/baseline", json={"user_id": 1, "context": [3], "exclude": [7], "k": 20}
        )

    assert storage.calls == []


def test_baseline_honors_k():
    with _baseline_client() as client:
        for k in (20, 100, 500):
            resp = client.post(
                "/v1/baseline", json={"user_id": 1, "context": [], "exclude": [], "k": k}
            )
            assert resp.json()["returned_count"] == k


def test_baseline_payload_matches_real_response_size():
    """Guarda central: o piso so e subtraivel da latencia de uma celula se o
    payload tiver tamanho equivalente ao de uma resposta real — o custo de
    serializacao e de rede escala com bytes. Compara contra uma resposta real
    de E-1 com ids e scores de largura realista (item ids do MovieLens vao ate
    87.585; scores do ALS tem varias casas decimais)."""
    real_candidates = [
        Candidate(item_id=10_000 + i * 137, score=round(0.913_47 - i * 0.000_991, 6))
        for i in range(20)
    ]
    storage = FakeStorageAdapter(candidates_by_user={1: real_candidates})
    payload = {"user_id": 1, "context": [], "exclude": [], "k": 20}

    app = create_app(E1AppFilter(), storage)
    with TestClient(app) as client:
        real = len(json.dumps(client.post("/v1/recommendations", json=payload).json()))
        baseline = len(json.dumps(client.post("/v1/baseline", json=payload).json()))

    assert abs(real - baseline) / real < 0.05, (
        f"payload do baseline ({baseline} bytes) diverge do real ({real} bytes) "
        "em mais de 5% — o piso deixa de ser comparavel"
    )
