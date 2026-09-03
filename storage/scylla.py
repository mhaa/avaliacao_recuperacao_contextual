"""Adaptador ScyllaDB (BD-3).

E-4 é inviável aqui por CONTEXTO.md: sem primitiva de interseção de
conjuntos em CQL. `supported_primitives` não inclui `INTERSECT` — a classe
base já levanta `PrimitiveNotSupported` por padrão se algo tentar chamar.

Modelagem (CQL não tem joins nem filtro arbitrário fora da chave de
partição/clustering, salvo `ALLOW FILTERING`, evitado aqui):

- `candidates(user_id, rank)` — PK (user_id, rank), leitura direta de todos
  os candidatos do usuário.
- `item_contexts(item_id, context_id)` — pertença item->contexto, varrida
  por INTEIRO uma única vez na montagem da célula (`load_item_contexts`),
  para o catálogo em memória de `core/catalog.py`.

  `get_candidates` (E-1) NÃO a consulta mais por requisição: emitia 500
  consultas de partição única com janela de 64 em voo, ou seja ~8 ondas
  sequenciais de round-trip por requisição de API, o que fazia a célula
  E-1/Scylla medir a normalização escolhida aqui em vez do custo real de
  ler uma partição de candidatos (ver CONTEXTO.md, "Catálogo
  item->contexto residente na aplicação").
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
from cassandra.query import SimpleStatement, dict_factory

from core.contract import Candidate

from .base import (
    GET_CANDIDATES,
    GET_CANDIDATES_FILTERED,
    GET_PREMATERIALIZED,
    LOAD_ITEM_CONTEXTS,
    StorageAdapter,
)

KEYSPACE = "recsys"

# Páginas da varredura completa de item_contexts na carga do catálogo
# (~200 mil linhas, uma única vez na subida do serviço).
_CATALOG_FETCH_SIZE = 10_000


class ScyllaAdapter(StorageAdapter):
    name = "scylla"
    supported_primitives = frozenset(
        {GET_CANDIDATES, GET_CANDIDATES_FILTERED, GET_PREMATERIALIZED, LOAD_ITEM_CONTEXTS}
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
            self._stmt_by_context,
            self._stmt_prematerialized,
        ):
            statement.consistency_level = ConsistencyLevel.ONE

    async def get_candidates(self, user_id: int) -> list[Candidate]:
        return await asyncio.to_thread(self._get_candidates_sync, user_id)

    def _get_candidates_sync(self, user_id: int) -> list[Candidate]:
        rows = self._session.execute(self._stmt_candidates, (user_id,))
        return [Candidate(item_id=row["item_id"], score=row["score"]) for row in rows]

    async def load_item_contexts(self) -> dict[int, frozenset[int]]:
        return await asyncio.to_thread(self._load_item_contexts_sync)

    def _load_item_contexts_sync(self) -> dict[int, frozenset[int]]:
        # Varredura de tabela inteira — aceitável SÓ porque acontece uma vez
        # na montagem da célula. fetch_size explícito para o driver paginar
        # em vez de tentar materializar ~200 mil linhas de uma vez.
        statement = SimpleStatement(
            f"SELECT item_id, context_id FROM {KEYSPACE}.item_contexts",
            fetch_size=_CATALOG_FETCH_SIZE,
            consistency_level=ConsistencyLevel.ONE,
        )
        accumulator: dict[int, set[int]] = {}
        for row in self._session.execute(statement):
            accumulator.setdefault(row["item_id"], set()).add(row["context_id"])
        return {item_id: frozenset(contexts) for item_id, contexts in accumulator.items()}

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
