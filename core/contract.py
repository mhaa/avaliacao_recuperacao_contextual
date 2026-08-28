"""Modelos de requisição e resposta do serviço de recuperação.

Contrato fixo por CONTEXTO.md ("Contrato da API"): a resposta carrega só
item_id/score/rank, sem metadados descritivos, para manter o payload em
~800 bytes e evitar joins. `extra="forbid"` em todos os modelos garante que
um campo extra falhe alto na validação em vez de ser silenciosamente aceito.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

RESPONSE_BYTE_BUDGET = 800


class Candidate(BaseModel):
    """Candidato tal como circula entre storage/ e strategies/ — nunca vai
    para a rede diretamente (ver `ResponseItem`, que é o que sai no wire).

    `context_ids` é a pertença do item aos contextos do catálogo (dado de
    catálogo, não por usuário) — necessária para E-1 avaliar o predicado no
    processo do serviço sem depender do banco para isso.
    """

    model_config = ConfigDict(extra="forbid")

    item_id: int
    score: float
    context_ids: frozenset[int] = frozenset()


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    context: list[int]
    exclude: list[int]
    k: int


class ResponseItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int
    score: float
    rank: int


class Response(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ResponseItem]
    returned_count: int


def build_response(ranked_items: list[ResponseItem], k: int) -> Response:
    """Trunca em k; nunca completa de outra fonte se houver menos itens
    elegíveis que k — registra a contagem real em `returned_count`."""
    truncated = ranked_items[:k]
    return Response(items=truncated, returned_count=len(truncated))
