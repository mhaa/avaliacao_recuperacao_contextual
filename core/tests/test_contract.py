from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.contract import RESPONSE_BYTE_BUDGET, Request, Response, ResponseItem


def test_request_accepts_all_contract_fields():
    req = Request(user_id=1, context=[5], exclude=[9], k=20)
    assert req.user_id == 1
    assert req.context == [5]
    assert req.exclude == [9]
    assert req.k == 20


def test_request_rejects_unknown_field():
    with pytest.raises(ValidationError):
        Request(user_id=1, context=[], exclude=[], k=20, extra_field=True)


def test_request_missing_field_raises():
    with pytest.raises(ValidationError):
        Request(user_id=1, context=[], k=20)  # falta exclude


def test_response_item_has_no_descriptive_metadata():
    item = ResponseItem(item_id=1, score=0.5, rank=1)
    assert set(item.model_dump().keys()) == {"item_id", "score", "rank"}


def test_response_item_rejects_extra_metadata_field():
    with pytest.raises(ValidationError):
        ResponseItem(item_id=1, score=0.5, rank=1, title="Movie")


def test_k20_response_size_within_budget():
    # model_dump_json() usa separadores compactos (sem espaço), o mesmo
    # formato de um payload de rede real — json.dumps(model_dump()) infla o
    # tamanho com espaços que nenhum cliente HTTP real envia.
    items = [ResponseItem(item_id=i, score=1.0 - i * 0.001, rank=i + 1) for i in range(20)]
    response = Response(items=items, returned_count=20)
    payload_size = len(response.model_dump_json().encode("utf-8"))
    assert payload_size <= RESPONSE_BYTE_BUDGET, (
        f"payload de {payload_size} bytes excede o orçamento de "
        f"{RESPONSE_BYTE_BUDGET} bytes (CONTEXTO.md) — orçamento documentado, "
        "não um contrato rígido, mas um estouro grande é sinal de alerta."
    )
