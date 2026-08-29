"""Prova que o transporte HTTP não corrompe nada — algo que o harness,
por design, nunca exercita (ele chama strategy.retrieve diretamente, sem
passar por rede nenhuma). Manda alguns casos reais do oráculo para o
container `service` de verdade, via HTTP de verdade, e compara com o
esperado.

Exige, antes de rodar (mesma massa da suíte do harness):
    docker compose up -d postgres
    docker compose run --rm --entrypoint python tools schemas/postgres/load_oracle_fixture.py
    docker compose up -d service
    docker compose run --rm tools -m integration tests/acceptance/test_service_smoke.py -v

Também é o Gate 2 do smoke test em nuvem (infra/scripts/cloud_smoke_test.py,
build_remote_smoke_script) — mesma lógica, só que TEST_SERVICE_URL aponta
para o IP interno da VM de serviço da célula em vez de `service:8000`.
"""

from __future__ import annotations

import os

import httpx
import pytest

from harness.oracle import load_oracle_cases
from harness.verify import verify_case

pytestmark = pytest.mark.integration

SERVICE_URL = os.environ.get("TEST_SERVICE_URL", "http://service:8000")

# Uma amostra, não os 1000 — este teste prova que o HTTP não corrompe o
# contrato, a corretude em si já está provada pelo harness (sem HTTP).
_SAMPLE_SIZE = 20


class _FakeResponseItem:
    """Espelha core.contract.ResponseItem o suficiente para verify_case,
    a partir do JSON decodificado da resposta HTTP (sem depender de
    validação Pydantic aqui — é exatamente o byte que chegou pela rede
    que este teste quer conferir)."""

    def __init__(self, item_id: int, score: float):
        self.item_id = item_id
        self.score = score


async def test_http_responses_match_oracle_sample():
    cases = load_oracle_cases()[:_SAMPLE_SIZE]
    async with httpx.AsyncClient(base_url=SERVICE_URL, timeout=10.0) as client:
        failed = []
        for case in cases:
            resp = await client.post(
                "/v1/recommendations",
                json={
                    "user_id": case.user_id,
                    "context": case.context_ids,
                    "exclude": case.exclude_ids,
                    "k": case.k,
                },
            )
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert set(body.keys()) == {"items", "returned_count"}
            items = [_FakeResponseItem(i["item_id"], i["score"]) for i in body["items"]]
            result = verify_case(case, items)
            if not result.passed:
                failed.append((case.case_id, result.reason))

    assert not failed, f"{len(failed)}/{_SAMPLE_SIZE} casos falharam via HTTP: {failed[:5]}"
