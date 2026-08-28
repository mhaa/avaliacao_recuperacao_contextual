"""Carrega, em um Postgres já com o esquema aplicado, os dados de
harness/fixtures.py necessários para verificar os 1000 casos do oráculo.

Uso:
    docker compose up -d postgres
    docker compose run --rm --entrypoint python tools schemas/postgres/load_oracle_fixture.py
"""

from __future__ import annotations

import os

import psycopg

from harness import fixtures

CONNINFO = os.environ.get("TEST_POSTGRES_DSN", "postgresql://tcc:tcc@postgres:5432/recsys")


def main() -> None:
    user_ids = fixtures.needed_user_ids()
    candidates = fixtures.load_candidates(user_ids)
    item_contexts = fixtures.load_item_contexts()
    prematerialized = fixtures.load_prematerialized(user_ids)

    with psycopg.connect(CONNINFO, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE candidates, item_contexts, prematerialized")
            with cur.copy("COPY candidates (user_id, item_id, rank, score) FROM STDIN") as copy:
                for row in candidates.select(["user_id", "item_id", "rank", "score"]).iter_rows():
                    copy.write_row(row)
            with cur.copy("COPY item_contexts (item_id, context_id) FROM STDIN") as copy:
                for row in item_contexts.iter_rows():
                    copy.write_row(row)
            with cur.copy(
                "COPY prematerialized (user_id, context_id, item_id, rank, score) FROM STDIN"
            ) as copy:
                for row in prematerialized.select(
                    ["user_id", "context_id", "item_id", "rank", "score"]
                ).iter_rows():
                    copy.write_row(row)

    print(
        f"Carregado: {len(candidates)} candidatos, {len(item_contexts)} pertences "
        f"item-contexto, {len(prematerialized)} linhas pré-materializadas, "
        f"{len(user_ids)} usuários referenciados pelo oráculo"
    )


if __name__ == "__main__":
    main()
