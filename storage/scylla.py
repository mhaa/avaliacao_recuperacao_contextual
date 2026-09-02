"""Adaptador ScyllaDB (BD-3).

E-4 é inviável aqui por CONTEXTO.md: sem primitiva de interseção de
conjuntos em CQL. `supported_primitives` não inclui `INTERSECT` — a classe
base já levanta `PrimitiveNotSupported` por padrão se algo tentar chamar.

Modelagem (CQL não tem joins nem filtro arbitrário fora da chave de
partição/clustering, salvo `ALLOW FILTERING`, evitado aqui):

- `candidates(user_id, rank)` — PK (user_id, rank), leitura direta de todos
  os candidatos do usuário.
- `item_contexts(item_id, context_id)` — pertença item->contexto, usada por
  `get_candidates` para popular `context_ids` (uma consulta de partição
  única por item, disparadas concorrentemente — ver `_get_candidates_sync`).
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
pensado para Cluster/Session de vida longa) e reaproveitada, com todas as
consultas como prepared statements preparados junto — o driver já gerencia
seu próprio pool de conexões por host, então não há nada a somar aqui
(diferente de storage/postgres.py, onde o pool é explícito).
"""

from __future__ import annotations

import asyncio

from cassandra import ConsistencyLevel
from cassandra.cluster import Cluster
from cassandra.concurrent import execute_concurrent_with_args
from cassandra.query import dict_factory

from core.contract import Candidate

from .base import GET_CANDIDATES, GET_CANDIDATES_FILTERED, GET_PREMATERIALIZED, StorageAdapter

KEYSPACE = "recsys"

# Consultas de item_contexts em voo por chamada de get_candidates (E-1 lê
# até N=500 itens). Ver _get_candidates_sync para o porquê de serem
# consultas de partição única concorrentes em vez de um IN multi-partição.
_ITEM_CONTEXTS_CONCURRENCY = 64


class ScyllaAdapter(StorageAdapter):
    name = "scylla"
    supported_primitives = frozenset(
        {GET_CANDIDATES, GET_CANDIDATES_FILTERED, GET_PREMATERIALIZED}
    )

    def __init__(self, hosts: list[str]):
        self._cluster = Cluster(hosts)
        self._session = self._cluster.connect(KEYSPACE)
        self._session.row_factory = dict_factory
        # Prepared statements, preparados uma vez na montagem da célula: é
        # a prática recomendada nº1 do Cassandra/Scylla no caminho quente.
        # Sem preparar, o driver não sabe onde fica a partition key e perde
        # o roteamento token-aware (TokenAwarePolicy, que já é o default do
        # cassandra-driver, vira round-robin na prática), além de o servidor
        # reparsear o CQL a cada requisição. Os loaders (schemas/scylla/)
        # já faziam isso; o caminho de leitura, não.
        self._stmt_candidates = self._session.prepare(
            "SELECT item_id, score FROM candidates WHERE user_id = ?"
        )
        self._stmt_item_contexts = self._session.prepare(
            "SELECT item_id, context_id FROM item_contexts WHERE item_id = ?"
        )
        self._stmt_by_context = self._session.prepare(
            "SELECT item_id, score FROM candidates_by_context "
            "WHERE context_id = ? AND user_id = ?"
        )
        self._stmt_prematerialized = self._session.prepare(
            "SELECT item_id, score FROM prematerialized WHERE user_id = ? AND context_id = ?"
        )
        # Explícito em vez de herdar o default do driver: com
        # replication_factor=1 (schemas/scylla/apply_schema.py, nó único)
        # ONE é o único nível que faz sentido — deixar registrado evita que
        # uma mudança de default do driver altere silenciosamente o que a
        # medição está medindo.
        for statement in (
            self._stmt_candidates,
            self._stmt_item_contexts,
            self._stmt_by_context,
            self._stmt_prematerialized,
        ):
            statement.consistency_level = ConsistencyLevel.ONE

    async def get_candidates(self, user_id: int) -> list[Candidate]:
        return await asyncio.to_thread(self._get_candidates_sync, user_id)

    def _get_candidates_sync(self, user_id: int) -> list[Candidate]:
        rows = list(self._session.execute(self._stmt_candidates, (user_id,)))
        if not rows:
            return []
        item_ids = [row["item_id"] for row in rows]
        # Uma consulta de PARTIÇÃO ÚNICA por item, disparadas
        # concorrentemente — não um `IN` sobre até 100 chaves de partição
        # por vez (que era o que estava aqui). `IN` multi-partição é
        # anti-pattern documentado em Cassandra/Scylla: o coordenador vira
        # ponto único de fan-out e segura todos os resultados em memória,
        # enquanto consultas de partição única são roteadas direto ao dono
        # de cada token e falham/repetem de forma independente. Também
        # elimina os 5 round-trips SEQUENCIAIS que uma leitura de N=500
        # itens custava (500 / 100 por lote), cada um segurando a thread
        # do executor até voltar.
        contexts_by_item: dict[int, set[int]] = {}
        results = execute_concurrent_with_args(
            self._session,
            self._stmt_item_contexts,
            [(item_id,) for item_id in item_ids],
            concurrency=_ITEM_CONTEXTS_CONCURRENCY,
            raise_on_first_error=True,
        )
        for _success, context_rows in results:
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
            rows = self._session.execute(self._stmt_by_context, (context_id, user_id))
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
            self._stmt_prematerialized, (user_id, int(context_key))
        )
        return [Candidate(item_id=row["item_id"], score=row["score"]) for row in rows]
