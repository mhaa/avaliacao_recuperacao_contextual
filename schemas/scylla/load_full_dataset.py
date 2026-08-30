"""Carrega, em um ScyllaDB já com o esquema aplicado, a base completa
(todos os usuários, não só o subconjunto do oráculo — ver
schemas/scylla/load_oracle_fixture.py para esse) — usado pela bateria de
medição real da Fase 5 (infra/scripts/run_measurement_battery.py), nunca
pelo smoke test. Ver storage/scylla.py para o esquema de tabelas.

Uso:
    docker compose run --rm --entrypoint python tools schemas/scylla/load_full_dataset.py
"""

from __future__ import annotations

import os

from cassandra.cluster import Cluster
from cassandra.concurrent import execute_concurrent, execute_concurrent_with_args
from cassandra.query import BatchStatement, BatchType

from harness import fixtures

HOSTS = os.environ.get("TEST_SCYLLA_HOSTS", "scylla").split(",")
KEYSPACE = "recsys"

# Linhas por lote dentro de uma mesma partição — mesmo motivo de
# load_oracle_fixture.py, mais crítico ainda em escala real (partições bem
# maiores por contexto de alta seletividade).
_BATCH_CHUNK_SIZE = 100


def _chunked(rows: list, size: int):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def main() -> None:
    fixtures.ensure_full_dataset_downloaded()

    cluster = Cluster(HOSTS)
    session = cluster.connect(KEYSPACE)
    session.default_timeout = 60.0

    for table in ("candidates", "item_contexts", "candidates_by_context", "prematerialized"):
        session.execute(f"TRUNCATE {table}")

    candidates = fixtures.load_candidates()
    item_contexts = fixtures.load_item_contexts()
    prematerialized = fixtures.load_prematerialized()

    insert_candidates = session.prepare(
        "INSERT INTO candidates (user_id, rank, item_id, score) VALUES (?, ?, ?, ?)"
    )
    execute_concurrent_with_args(
        session,
        insert_candidates,
        candidates.select(["user_id", "rank", "item_id", "score"]).iter_rows(),
        concurrency=20,
    )

    insert_item_contexts = session.prepare(
        "INSERT INTO item_contexts (item_id, context_id) VALUES (?, ?)"
    )
    execute_concurrent_with_args(
        session, insert_item_contexts, item_contexts.iter_rows(), concurrency=100
    )

    by_context = candidates.select(["user_id", "item_id", "rank", "score"]).join(
        item_contexts, on="item_id", how="inner"
    )
    insert_by_context = session.prepare(
        "INSERT INTO candidates_by_context (context_id, user_id, rank, item_id, score) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    batches = []
    for (context_id, user_id), group in by_context.group_by(["context_id", "user_id"]):
        rows = group.select(["rank", "item_id", "score"]).rows()
        for chunk in _chunked(rows, _BATCH_CHUNK_SIZE):
            batch = BatchStatement(batch_type=BatchType.UNLOGGED)
            for rank, item_id, score in chunk:
                batch.add(insert_by_context, (context_id, user_id, rank, item_id, score))
            batches.append((batch, None))
    execute_concurrent(session, batches, concurrency=10)

    insert_prematerialized = session.prepare(
        "INSERT INTO prematerialized (user_id, context_id, rank, item_id, score) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    execute_concurrent_with_args(
        session,
        insert_prematerialized,
        prematerialized.select(
            ["user_id", "context_id", "rank", "item_id", "score"]
        ).iter_rows(),
        concurrency=20,
    )

    cluster.shutdown()

    print(
        f"Carregado: {candidates.height} candidatos, {item_contexts.height} pertences "
        f"item-contexto, {by_context.height} linhas desnormalizadas por contexto, "
        f"{prematerialized.height} linhas pré-materializadas (base completa)"
    )


if __name__ == "__main__":
    main()
