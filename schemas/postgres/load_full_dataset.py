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


def _copy_rows(copy, rows_iter, label: str, interval: int) -> None:
    """Envolve `copy.write_row()` com print periódico de progresso — sem
    isso, COPY transmite em silêncio total até acabar. Numa carga real
    (dezenas de milhões de linhas) isso é indistinguível de travado visto
    de fora; mesmo raciocínio do `label` em
    schemas/scylla/load_full_dataset.py._execute_concurrent_batched."""
    count = 0
    for row in rows_iter:
        copy.write_row(row)
        count += 1
        if count % interval == 0:
            print(f"{label}: {count} linhas gravadas", flush=True)
    if count:
        print(f"{label}: {count} linhas gravadas (final)", flush=True)


def main() -> None:
    fixtures.ensure_full_dataset_downloaded()

    candidates = fixtures.load_candidates()
    item_contexts = fixtures.load_item_contexts()
    prematerialized = fixtures.load_prematerialized()
    inverted_lists = fixtures.load_inverted_lists()

    with psycopg.connect(CONNINFO, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE candidates, item_contexts, prematerialized, inverted_lists"
            )
            with cur.copy("COPY candidates (user_id, item_id, rank, score) FROM STDIN") as copy:
                _copy_rows(
                    copy,
                    candidates.select(["user_id", "item_id", "rank", "score"]).iter_rows(),
                    label="candidates",
                    interval=2_000_000,
                )
            with cur.copy("COPY item_contexts (item_id, context_id) FROM STDIN") as copy:
                _copy_rows(
                    copy, item_contexts.iter_rows(), label="item_contexts", interval=500_000
                )
            with cur.copy(
                "COPY prematerialized (user_id, context_id, item_id, rank, score) FROM STDIN"
            ) as copy:
                _copy_rows(
                    copy,
                    prematerialized.select(
                        ["user_id", "context_id", "item_id", "rank", "score"]
                    ).iter_rows(),
                    label="prematerialized",
                    interval=500_000,
                )
            # ~20 linhas (C=20 contextos) — INSERT parametrizado em vez de
            # COPY: psycopg3 adapta list[int] para int[] diretamente via
            # parâmetro, sem depender da serialização de array do protocolo
            # texto do COPY (mesma razão de load_oracle_fixture.py).
            print("inverted_lists: gravando...", flush=True)
            cur.executemany(
                "INSERT INTO inverted_lists (context_id, item_ids) VALUES (%s, %s)",
                inverted_lists.select(["context_id", "item_ids"]).iter_rows(),
            )
            print(f"inverted_lists: {len(inverted_lists)} linhas gravadas (final)", flush=True)
            # VACUUM ANALYZE (não só ANALYZE) depois do COPY em massa, por
            # dois motivos distintos:
            # - ANALYZE: sem estatística, o planner escolhe plano no escuro
            #   até o autovacuum decidir analisar sozinho — o que pode
            #   acontecer NO MEIO da bateria, trocando o plano na metade das
            #   repetições.
            # - VACUUM: preenche o mapa de visibilidade, que é o que permite
            #   ao index-only scan PULAR a heap. Sem ele os índices covering
            #   (INCLUDE em 001_schema.sql/002_prematerialized.sql) ainda
            #   fazem Heap Fetches em toda linha e o INCLUDE não paga nada —
            #   confirmado com EXPLAIN (ANALYZE, BUFFERS) numa base recém-
            #   carregada.
            print("Rodando VACUUM ANALYZE...", flush=True)
            cur.execute(
                "VACUUM ANALYZE candidates, item_contexts, prematerialized, inverted_lists"
            )

    print(
        f"Carregado: {len(candidates)} candidatos, {len(item_contexts)} pertences "
        f"item-contexto, {len(prematerialized)} linhas pré-materializadas, "
        f"{len(inverted_lists)} listas invertidas de contexto (base completa)"
    )


if __name__ == "__main__":
    main()
