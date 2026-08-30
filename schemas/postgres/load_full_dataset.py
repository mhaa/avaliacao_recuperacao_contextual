"""Carrega, em um Postgres já com o esquema aplicado, a base completa
(todos os usuários, não só o subconjunto do oráculo — ver
schemas/postgres/load_oracle_fixture.py para esse) — usado pela bateria de
medição real da Fase 5 (infra/scripts/run_measurement_battery.py), nunca
pelo smoke test.

Uso:
    docker compose run --rm --entrypoint python tools schemas/postgres/load_full_dataset.py
"""

from __future__ import annotations

import os

import psycopg

from harness import fixtures

CONNINFO = os.environ.get("TEST_POSTGRES_DSN", "postgresql://tcc:tcc@postgres:5432/recsys")


def main() -> None:
    fixtures.ensure_full_dataset_downloaded()

    candidates = fixtures.load_candidates()
    item_contexts = fixtures.load_item_contexts()
    prematerialized = fixtures.load_prematerialized()

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
        f"item-contexto, {len(prematerialized)} linhas pré-materializadas (base completa)"
    )


if __name__ == "__main__":
    main()
