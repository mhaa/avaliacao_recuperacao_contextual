"""Adaptador ScyllaDB (BD-3).

E-4 é inviável aqui por CONTEXTO.md: sem primitiva de interseção de
conjuntos em CQL. `supported_primitives` não inclui `INTERSECT` — a classe
base já levanta `PrimitiveNotSupported` por padrão se algo tentar chamar.

Modelagem (CQL não tem joins nem filtro arbitrário fora da chave de
partição/clustering, salvo `ALLOW FILTERING`, evitado aqui):

- `candidates(user_id, rank)` — PK (user_id, rank), leitura direta de todos
  os candidatos do usuário.
- `item_contexts(item_id, context_id)` — pertença item->contexto, usada por
  `get_candidates` para popular `context_ids` (busca em lote via `IN`).
- `candidates_by_context((context_id, user_id), rank)` — tabela
  desnormalizada, partição por (contexto, usuário): leitura direta e JÁ
  FILTRADA por um único contexto (E-2). Cada leitura devolve o conjunto
  COMPLETO (não truncado) de candidatos do usuário naquele contexto — por
  isso, para contexto composto, `get_candidates_filtered` lê uma partição
  por context_id pedido e intersecta em Python; ao contrário do top-40 de
  E-3, isso é correto (nenhuma leitura é truncada).
- `prematerialized((user_id, context_id), rank)` — chave composta, leitura
  direta para E-3 (mesma limitação de contexto único das outras adaptações).

Sessão aberta uma vez no construtor (padrão usual do cassandra-driver,
pensado para Cluster/Session de vida longa) e reaproveitada — diferente de
storage/postgres.py (conexão por chamada), mas cada driver tem seu
idiomático; sem pool "de verdade" em nenhum dos dois nesta etapa, focada em
corretude.
"""

from __future__ import annotations

import asyncio

from cassandra.cluster import Cluster
from cassandra.query import dict_factory

from core.contract import Candidate

from .base import GET_CANDIDATES, GET_CANDIDATES_FILTERED, GET_PREMATERIALIZED, StorageAdapter

KEYSPACE = "recsys"


def _chunked(values: list, size: int):
    for i in range(0, len(values), size):
        yield values[i : i + size]


class ScyllaAdapter(StorageAdapter):
    name = "scylla"
    supported_primitives = frozenset(
        {GET_CANDIDATES, GET_CANDIDATES_FILTERED, GET_PREMATERIALIZED}
    )

    def __init__(self, hosts: list[str]):
        self._cluster = Cluster(hosts)
        self._session = self._cluster.connect(KEYSPACE)
        self._session.row_factory = dict_factory

    async def get_candidates(self, user_id: int) -> list[Candidate]:
        return await asyncio.to_thread(self._get_candidates_sync, user_id)

    def _get_candidates_sync(self, user_id: int) -> list[Candidate]:
        rows = list(
            self._session.execute(
                "SELECT item_id, score FROM candidates WHERE user_id = %s", (user_id,)
            )
        )
        if not rows:
            return []
        item_ids = [row["item_id"] for row in rows]
        # Um único "%s" para o IN inteiro (passando list/tuple como valor)
        # dá erro de sintaxe no parser do Scylla — um placeholder "%s" por
        # valor, construído no tamanho do lote, é o que funciona. E o
        # Scylla limita IN de chave de partição a 100 valores por consulta
        # por padrão — um usuário pode ter até N=500 candidatos (ver
        # CONTEXTO.md), então isso precisa ser feito em lotes.
        contexts_by_item: dict[int, set[int]] = {}
        for chunk in _chunked(item_ids, 100):
            placeholders = ", ".join(["%s"] * len(chunk))
            context_rows = self._session.execute(
                f"SELECT item_id, context_id FROM item_contexts WHERE item_id IN ({placeholders})",
                tuple(chunk),
            )
            for row in context_rows:
                contexts_by_item.setdefault(row["item_id"], set()).add(row["context_id"])
        return [
            Candidate(
                item_id=row["item_id"],
                score=row["score"],
                context_ids=frozenset(contexts_by_item.get(row["item_id"], set())),
            )
            for row in rows
        ]

    async def get_candidates_filtered(self, user_id: int, context: list[int]) -> list[Candidate]:
        if not context:
            return await self.get_candidates(user_id)
        return await asyncio.to_thread(self._get_candidates_filtered_sync, user_id, context)

    def _get_candidates_filtered_sync(self, user_id: int, context: list[int]) -> list[Candidate]:
        intersection: dict[int, float] | None = None
        for context_id in context:
            rows = self._session.execute(
                "SELECT item_id, score FROM candidates_by_context "
                "WHERE context_id = %s AND user_id = %s",
                (context_id, user_id),
            )
            this_context = {row["item_id"]: row["score"] for row in rows}
            intersection = (
                this_context
                if intersection is None
                else {iid: s for iid, s in intersection.items() if iid in this_context}
            )
        intersection = intersection or {}
        return [Candidate(item_id=iid, score=s) for iid, s in intersection.items()]

    async def get_prematerialized(self, user_id: int, context_key: str) -> list[Candidate]:
        return await asyncio.to_thread(self._get_prematerialized_sync, user_id, context_key)

    def _get_prematerialized_sync(self, user_id: int, context_key: str) -> list[Candidate]:
        rows = self._session.execute(
            "SELECT item_id, score FROM prematerialized WHERE user_id = %s AND context_id = %s",
            (user_id, int(context_key)),
        )
        return [Candidate(item_id=row["item_id"], score=row["score"]) for row in rows]
