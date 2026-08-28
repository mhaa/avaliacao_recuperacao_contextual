"""Adaptador Postgres (BD-1).

Implementa as 4 primitivas. `get_candidates`/`get_candidates_filtered`
chegaram na Etapa 2; `get_prematerialized`/`intersect` chegam agora (Etapa
4) — todas as 4 são viáveis em Postgres por CONTEXTO.md.

`intersect` reaproveita a mesma consulta relacional de
`get_candidates_filtered` (via `item_contexts`), só com `LIMIT`. Isso é
suficiente para corretude — o ponto desta etapa — mas não modela ainda a
distinção arquitetural que a Fase 2 do TCC vai medir (predicado via WHERE
vs. interseção de conjuntos via `intarray`/bitmap); ajustar a técnica SQL
de `intersect` fica para quando a comparação de latência entrar em cena,
sem mudar a interface pública.

Abre uma conexão por chamada (sem pool) — aceitável nesta etapa, focada em
corretude; pooling é uma otimização de performance que pode ser adicionada
depois sem mudar a interface.
"""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from core.contract import Candidate

from .base import GET_CANDIDATES, GET_CANDIDATES_FILTERED, GET_PREMATERIALIZED, INTERSECT, StorageAdapter

_GET_CANDIDATES_SQL = """
    SELECT c.item_id, c.score,
           COALESCE(
               array_agg(ic.context_id) FILTER (WHERE ic.context_id IS NOT NULL),
               '{}'
           ) AS context_ids
    FROM candidates c
    LEFT JOIN item_contexts ic ON ic.item_id = c.item_id
    WHERE c.user_id = %(user_id)s
    GROUP BY c.item_id, c.score, c.rank
    ORDER BY c.rank
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

_INTERSECT_SQL = _GET_CANDIDATES_FILTERED_SQL + "\n    LIMIT %(limit)s"

_GET_PREMATERIALIZED_SQL = """
    SELECT item_id, score
    FROM prematerialized
    WHERE user_id = %(user_id)s AND context_id = %(context_id)s
    ORDER BY rank
"""


class PostgresAdapter(StorageAdapter):
    name = "postgres"
    supported_primitives = frozenset(
        {GET_CANDIDATES, GET_CANDIDATES_FILTERED, GET_PREMATERIALIZED, INTERSECT}
    )

    def __init__(self, conninfo: str):
        self._conninfo = conninfo

    async def get_candidates(self, user_id: int) -> list[Candidate]:
        async with await psycopg.AsyncConnection.connect(
            self._conninfo, row_factory=dict_row
        ) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_GET_CANDIDATES_SQL, {"user_id": user_id})
                rows = await cur.fetchall()
        return [
            Candidate(
                item_id=row["item_id"],
                score=row["score"],
                context_ids=frozenset(row["context_ids"]),
            )
            for row in rows
        ]

    async def get_candidates_filtered(self, user_id: int, context: list[int]) -> list[Candidate]:
        if not context:
            return await self.get_candidates(user_id)
        async with await psycopg.AsyncConnection.connect(
            self._conninfo, row_factory=dict_row
        ) as conn:
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
        async with await psycopg.AsyncConnection.connect(
            self._conninfo, row_factory=dict_row
        ) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    _GET_PREMATERIALIZED_SQL,
                    {"user_id": user_id, "context_id": int(context_key)},
                )
                rows = await cur.fetchall()
        return [Candidate(item_id=row["item_id"], score=row["score"]) for row in rows]

    async def intersect(self, user_id: int, context: list[int], limit: int) -> list[Candidate]:
        async with await psycopg.AsyncConnection.connect(
            self._conninfo, row_factory=dict_row
        ) as conn:
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
