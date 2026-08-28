"""Adaptador OpenSearch (BD-4).

E-3 (pré-materialização) não tem sentido arquitetural aqui por CONTEXTO.md:
pré-materializar respostas chave-valor não aproveita nada do que um índice
invertido oferece. `get_prematerialized` não é implementado —
comportamento padrão da classe base levanta `PrimitiveNotSupported`, e
`supported_primitives` não inclui essa primitiva.

Índice "candidates": um documento por (user_id, item_id), com context_ids
como campo numérico multi-valor. `get_candidates_filtered` usa uma bool
query com uma cláusula `term` por context_id pedido (AND via múltiplas
cláusulas `must`) — exatamente a semântica que o índice invertido do Lucene
resolve nativamente, sem workaround (E-2 "nativo" por CONTEXTO.md).
`intersect` usa a mesma técnica de consulta por ora — a distinção real de
E-2 vs. E-4 (predicado direto vs. interseção com lista invertida global) é
uma otimização de Fase 2 (medição de latência), fora do escopo desta etapa
(corretude) — mesma decisão já tomada para Postgres/Scylla/Valkey.
"""

from __future__ import annotations

import asyncio

from opensearchpy import OpenSearch

from core.contract import Candidate

from .base import GET_CANDIDATES, GET_CANDIDATES_FILTERED, INTERSECT, StorageAdapter

INDEX = "candidates"
_N_CANDIDATES = 500


class OpenSearchAdapter(StorageAdapter):
    name = "opensearch"
    supported_primitives = frozenset({GET_CANDIDATES, GET_CANDIDATES_FILTERED, INTERSECT})

    def __init__(self, hosts: list[str]):
        self._client = OpenSearch(hosts=hosts, use_ssl=False, verify_certs=False)

    async def get_candidates(self, user_id: int) -> list[Candidate]:
        return await asyncio.to_thread(self._search, user_id, [], _N_CANDIDATES)

    async def get_candidates_filtered(self, user_id: int, context: list[int]) -> list[Candidate]:
        return await asyncio.to_thread(self._search, user_id, context, _N_CANDIDATES)

    async def intersect(self, user_id: int, context: list[int], limit: int) -> list[Candidate]:
        if not context:
            return []
        return await asyncio.to_thread(self._search, user_id, context, limit)

    def _search(self, user_id: int, context: list[int], size: int) -> list[Candidate]:
        must = [{"term": {"user_id": user_id}}]
        must.extend({"term": {"context_ids": context_id}} for context_id in context)
        body = {"query": {"bool": {"must": must}}, "size": size}
        response = self._client.search(index=INDEX, body=body)
        return [
            Candidate(
                item_id=hit["_source"]["item_id"],
                score=hit["_source"]["score"],
                context_ids=frozenset(hit["_source"].get("context_ids", [])),
            )
            for hit in response["hits"]["hits"]
        ]
