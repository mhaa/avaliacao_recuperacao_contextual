"""Adaptador Postgres (BD-1).

Implementa as 4 primitivas. `get_candidates`/`get_candidates_filtered`
chegaram na Etapa 2; `get_prematerialized`/`intersect` chegam agora (Etapa
4) — todas as 4 são viáveis em Postgres por docs/DESIGN.md.

`intersect` usa o módulo `intarray` (contrib oficial do Postgres, habilitado
em `schemas/postgres/003_inverted_lists.sql`) sobre uma lista invertida
global por contexto (`inverted_lists`, uma linha por contexto com TODOS os
seus itens, não truncada) — mecanismo genuinamente diferente do
JOIN+GROUP BY/HAVING de `get_candidates_filtered` (E-2): array-merge sobre
dado desnormalizado, não join linha-a-linha sobre `item_contexts`. Isso
resolve o placeholder anterior (que reaproveitava a SQL de E-2 com um
`LIMIT` colado, suficiente só para o harness de corretude, mas inaceitável
para a triagem — E-2 e E-4 mediriam o mesmo plano de execução). Ver
docs/DECISIONS.md, "Etapa 4", para a justificativa de não usar
`pg_roaringbitmap` (exigiria imagem Postgres customizada nos dois
ambientes, ver docker-compose.yml/infra/modules/database) nem índice GIN
(o acesso a `inverted_lists` é sempre direto por `context_id`, chave
primária).

O operador `&` do `intarray` (interseção de dois `int[]`) exige os dois
arrays ORDENADOS e sem duplicatas — por isso todo `array_agg` aqui usa
`ORDER BY item_id` explícito; sem isso o resultado da interseção seria
incorreto silenciosamente, não um erro.

Usa um `AsyncConnectionPool` (não uma conexão por chamada): medido ao vivo
na nuvem, abrir uma conexão nova por requisição estourava
`max_connections=200` do Postgres sob carga real (centenas de VUs
concorrentes no k6, cada um tentando abrir sua própria conexão) — a maioria
das requisições voltava HTTP 500 ("too many clients already") em vez de
medir latência de verdade. `max_size` fica bem abaixo de 200: cada célula
tem um único serviço falando com o banco, então isso já dá folga larga de
concorrência sem arriscar esgotar o limite do servidor.
"""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from core.contract import Candidate

from .base import (
    GET_CANDIDATES,
    GET_CANDIDATES_FILTERED,
    GET_PREMATERIALIZED,
    INTERSECT,
    LOAD_ITEM_CONTEXTS,
    StorageAdapter,
)

# Sem JOIN com item_contexts e sem ORDER BY: a pertença ao contexto vem do
# catálogo em memória (core/catalog.py, carregado uma vez por
# _LOAD_ITEM_CONTEXTS_SQL) e a ordenação final é sempre refeita por
# core/ordering.py:order_candidates — ordenar aqui era trabalho jogado fora,
# que nenhum dos outros três adaptadores pagava. Sobra um index-only scan
# sobre idx_candidates_user_rank (INCLUDE (item_id, score)).
_GET_CANDIDATES_SQL = """
    SELECT item_id, score
    FROM candidates
    WHERE user_id = %(user_id)s
"""

# Varredura única, na montagem da célula — nunca no caminho de requisição.
_LOAD_ITEM_CONTEXTS_SQL = """
    SELECT item_id, array_agg(context_id) AS context_ids
    FROM item_contexts
    GROUP BY item_id
"""

# Semântica AND: um item só entra se pertencer a TODOS os context_ids
# pedidos (mesma regra do oráculo — interseção, não união).
_GET_CANDIDATES_FILTERED_SQL = """
    SELECT c.item_id, c.score
    FROM candidates c
    JOIN item_contexts ic ON ic.item_id = c.item_id AND ic.context_id = ANY(%(context_ids)s)
    WHERE c.user_id = %(user_id)s
    GROUP BY c.item_id, c.score, c.rank
    HAVING COUNT(DISTINCT ic.context_id) = %(n_contexts)s
    ORDER BY MIN(c.rank)
"""

# Ver docstring do módulo: `&` (intarray) exige arrays ordenados e sem
# duplicatas, por isso os dois `array_agg` abaixo usam `ORDER BY item_id`.
# `context_intersection` reduz os N contextos pedidos ao seu AND (semântica
# igual às demais estratégias: um item só entra se aparecer nas N listas
# invertidas pedidas) antes de intersectar com os candidatos do usuário —
# essa redução usa GROUP BY/HAVING sobre `inverted_lists` (dado
# desnormalizado, uma linha por contexto), não sobre `item_contexts` (dado
# normalizado que E-2 usa), então continua sendo um caminho de execução
# distinto de E-2 mesmo nesse passo intermediário.
_INTERSECT_SQL = """
    WITH user_candidates AS (
        SELECT COALESCE(array_agg(item_id ORDER BY item_id), ARRAY[]::int[]) AS ids
        FROM candidates
        WHERE user_id = %(user_id)s
    ),
    context_intersection AS (
        SELECT COALESCE(array_agg(item_id ORDER BY item_id), ARRAY[]::int[]) AS ids
        FROM (
            SELECT item_id
            FROM (
                SELECT unnest(item_ids) AS item_id
                FROM inverted_lists
                WHERE context_id = ANY(%(context_ids)s)
            ) per_context
            GROUP BY item_id
            HAVING count(*) = %(n_contexts)s
        ) qualifying
    ),
    intersected AS (
        SELECT (user_candidates.ids & context_intersection.ids) AS ids
        FROM user_candidates, context_intersection
    )
    SELECT c.item_id, c.score
    FROM candidates c
    CROSS JOIN intersected
    WHERE c.user_id = %(user_id)s
      AND c.item_id = ANY(intersected.ids)
    LIMIT %(limit)s
"""

_GET_PREMATERIALIZED_SQL = """
    SELECT item_id, score
    FROM prematerialized
    WHERE user_id = %(user_id)s AND context_id = %(context_id)s
    ORDER BY rank
"""


class PostgresAdapter(StorageAdapter):
    name = "postgres"
    supported_primitives = frozenset(
        {
            GET_CANDIDATES,
            GET_CANDIDATES_FILTERED,
            GET_PREMATERIALIZED,
            INTERSECT,
            LOAD_ITEM_CONTEXTS,
        }
    )

    # max_connections=200 no Postgres (docker-compose.yml / infra/modules/
    # database) — max_size fica bem abaixo disso de propósito, ver
    # docstring do módulo.
    def __init__(self, conninfo: str, min_size: int = 4, max_size: int = 50):
        self._pool = AsyncConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            open=False,
            kwargs={"row_factory": dict_row},
        )

    async def close(self) -> None:
        await self._pool.close()

    async def get_candidates(self, user_id: int) -> list[Candidate]:
        await self._pool.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_GET_CANDIDATES_SQL, {"user_id": user_id})
                rows = await cur.fetchall()
        return [Candidate(item_id=row["item_id"], score=row["score"]) for row in rows]

    async def load_item_contexts(self) -> dict[int, frozenset[int]]:
        await self._pool.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(_LOAD_ITEM_CONTEXTS_SQL)
                rows = await cur.fetchall()
        return {row["item_id"]: frozenset(row["context_ids"]) for row in rows}

    async def get_candidates_filtered(self, user_id: int, context: list[int]) -> list[Candidate]:
        if not context:
            return await self.get_candidates(user_id)
        await self._pool.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    _GET_CANDIDATES_FILTERED_SQL,
                    {
                        "user_id": user_id,
                        "context_ids": list(context),
                        "n_contexts": len(set(context)),
                    },
                )
                rows = await cur.fetchall()
        return [Candidate(item_id=row["item_id"], score=row["score"]) for row in rows]

    async def get_prematerialized(self, user_id: int, context_key: str) -> list[Candidate]:
        await self._pool.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    _GET_PREMATERIALIZED_SQL,
                    {"user_id": user_id, "context_id": int(context_key)},
                )
                rows = await cur.fetchall()
        return [Candidate(item_id=row["item_id"], score=row["score"]) for row in rows]

    async def intersect(self, user_id: int, context: list[int], limit: int) -> list[Candidate]:
        # Sem lista invertida global para intersectar sem contexto — mesmo
        # contrato de storage/valkey.py e storage/opensearch.py (diferente
        # de get_candidates_filtered, onde contexto vazio devolve tudo).
        if not context:
            return []
        await self._pool.open()
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    _INTERSECT_SQL,
                    {
                        "user_id": user_id,
                        "context_ids": list(context),
                        "n_contexts": len(set(context)),
                        "limit": limit,
                    },
                )
                rows = await cur.fetchall()
        return [Candidate(item_id=row["item_id"], score=row["score"]) for row in rows]
