"""Adaptador OpenSearch (BD-4).

E-3 (pré-materialização) não tem sentido arquitetural aqui por CONTEXTO.md:
pré-materializar respostas chave-valor não aproveita nada do que um índice
invertido oferece. `get_prematerialized` não é implementado —
comportamento padrão da classe base levanta `PrimitiveNotSupported`, e
`supported_primitives` não inclui essa primitiva.

Índice "candidates": um documento por (user_id, item_id), com context_ids
como campo numérico multi-valor (desnormalizado na carga — é o que a query
`term` de E-2 resolve nativamente).

Índice "item_contexts": um documento por item (~87.585), lido em MASSA uma
única vez na montagem da célula (`load_item_contexts`) para o catálogo em
memória de `core/catalog.py`. Existe porque E-1 passou a filtrar contra
esse catálogo em vez de receber `context_ids` no caminho quente, e
reconstruir a pertença varrendo os ~100M documentos de `candidates` seria
inviável. Ver CONTEXTO.md, "Catálogo item->contexto residente na
aplicação". `get_candidates_filtered` usa uma bool
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

from .base import (
    GET_CANDIDATES,
    GET_CANDIDATES_FILTERED,
    INTERSECT,
    LOAD_ITEM_CONTEXTS,
    StorageAdapter,
)

INDEX = "candidates"
CATALOG_INDEX = "item_contexts"
_N_CANDIDATES = 500
# Página da varredura do catálogo. 10.000 é o teto default de `size` do
# OpenSearch (index.max_result_window) — com ~87.585 itens dá ~9 páginas,
# uma única vez na subida do serviço.
_CATALOG_PAGE_SIZE = 10_000


class OpenSearchAdapter(StorageAdapter):
    name = "opensearch"
    supported_primitives = frozenset(
        {GET_CANDIDATES, GET_CANDIDATES_FILTERED, INTERSECT, LOAD_ITEM_CONTEXTS}
    )

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
        # `filter`, não `must`: a semântica de correspondência é idêntica
        # (o predicado continua sendo resolvido pelo índice invertido, que é
        # o que define E-2 aqui), mas contexto de filtro pula o cálculo de
        # relevância BM25 e é cacheável. O `_score` do Lucene era descartado
        # de qualquer forma — quem ordena é core/ordering.py:rank_candidates,
        # pelo campo `score` (ALS) armazenado no documento.
        # track_total_hits=False evita contar todos os documentos que casam
        # além dos `size` devolvidos; `_source` enxuto evita trazer campo
        # que o adaptador não lê.
        filters = [{"term": {"user_id": user_id}}]
        filters.extend({"term": {"context_ids": context_id}} for context_id in context)
        body = {
            "query": {"bool": {"filter": filters}},
            "size": size,
            "track_total_hits": False,
            "_source": ["item_id", "score"],
        }
        response = self._client.search(index=INDEX, body=body)
        return [
            Candidate(item_id=hit["_source"]["item_id"], score=hit["_source"]["score"])
            for hit in response["hits"]["hits"]
        ]

    async def load_item_contexts(self) -> dict[int, frozenset[int]]:
        return await asyncio.to_thread(self._load_item_contexts_sync)

    def _load_item_contexts_sync(self) -> dict[int, frozenset[int]]:
        # search_after sobre item_id (não `from`/`size` paginado, que fica
        # O(n²), nem scroll, que segura um contexto de busca aberto no
        # servidor). Uma vez só, na montagem da célula.
        catalog: dict[int, frozenset[int]] = {}
        search_after: list | None = None
        while True:
            body: dict = {
                "query": {"match_all": {}},
                "size": _CATALOG_PAGE_SIZE,
                "sort": [{"item_id": "asc"}],
                "track_total_hits": False,
                "_source": ["item_id", "context_ids"],
            }
            if search_after is not None:
                body["search_after"] = search_after
            hits = self._client.search(index=CATALOG_INDEX, body=body)["hits"]["hits"]
            if not hits:
                return catalog
            for hit in hits:
                source = hit["_source"]
                catalog[source["item_id"]] = frozenset(source.get("context_ids", []))
            search_after = hits[-1]["sort"]
