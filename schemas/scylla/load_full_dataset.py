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
from cassandra.concurrent import execute_concurrent
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


def _execute_partitioned_batches(
    session,
    statement,
    df,
    partition_cols: list[str],
    other_cols: list[str],
    concurrency: int,
    label: str,
    flush_size: int = 2000,
) -> None:
    """Agrupa `df` pela(s) coluna(s) de partição e grava um BatchStatement
    por partição (nunca cruzando partições — Cassandra/Scylla trata batch
    de partição única com eficiência real, ao contrário de um batch
    espalhado). Isso importa MUITO em escala real: `candidates` sozinho
    tem ~100M linhas mas só ~200 mil usuários — inserir linha a linha (uma
    requisição de rede por linha, como antes) significa ~100M idas-e-
    voltas; agrupando por `user_id` (a própria chave de partição da
    tabela, PRIMARY KEY (user_id, rank)) vira ~200 mil lotes de até ~500
    linhas cada. Confirmado ao vivo: com a versão linha-a-linha, a carga
    real do e1-scylla passava horas com o processo Python saturado de CPU
    mesmo com o driver C-acelerado (cmurmur3/deserializers Cython
    presentes, verificado) — o gargalo era puramente o número de
    despachos Python por linha, não rede nem servidor. `candidates_by_context`
    já usava esse padrão (é o único motivo dele não ter o mesmo problema);
    generalizado aqui para as outras 3 tabelas.

    Flush a cada `flush_size` BatchStatements, não só no final — acumular
    tudo antes de um único execute_concurrent() no final arrisca OOM em
    escala real (~100M linhas)."""
    batches: list = []
    rows_done = 0
    for key, group in df.group_by(partition_cols):
        rows = group.select(other_cols).rows()
        for chunk in _chunked(rows, _BATCH_CHUNK_SIZE):
            batch = BatchStatement(batch_type=BatchType.UNLOGGED)
            for row in chunk:
                batch.add(statement, (*key, *row))
            batches.append((batch, None))
            rows_done += len(chunk)
            if len(batches) >= flush_size:
                execute_concurrent(session, batches, concurrency=concurrency)
                print(f"{label}: ~{rows_done} linhas gravadas", flush=True)
                batches = []
    if batches:
        execute_concurrent(session, batches, concurrency=concurrency)
        print(f"{label}: ~{rows_done} linhas gravadas (final)", flush=True)


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
    _execute_partitioned_batches(
        session,
        insert_candidates,
        candidates,
        partition_cols=["user_id"],
        other_cols=["rank", "item_id", "score"],
        concurrency=20,
        label="candidates",
    )

    insert_item_contexts = session.prepare(
        "INSERT INTO item_contexts (item_id, context_id) VALUES (?, ?)"
    )
    _execute_partitioned_batches(
        session,
        insert_item_contexts,
        item_contexts,
        partition_cols=["item_id"],
        other_cols=["context_id"],
        concurrency=20,
        label="item_contexts",
    )

    by_context = candidates.select(["user_id", "item_id", "rank", "score"]).join(
        item_contexts, on="item_id", how="inner"
    )
    insert_by_context = session.prepare(
        "INSERT INTO candidates_by_context (context_id, user_id, rank, item_id, score) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    _execute_partitioned_batches(
        session,
        insert_by_context,
        by_context,
        partition_cols=["context_id", "user_id"],
        other_cols=["rank", "item_id", "score"],
        concurrency=20,
        label="candidates_by_context",
    )

    insert_prematerialized = session.prepare(
        "INSERT INTO prematerialized (user_id, context_id, rank, item_id, score) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    _execute_partitioned_batches(
        session,
        insert_prematerialized,
        prematerialized,
        partition_cols=["user_id", "context_id"],
        other_cols=["rank", "item_id", "score"],
        concurrency=20,
        label="prematerialized",
    )

    cluster.shutdown()

    print(
        f"Carregado: {candidates.height} candidatos, {item_contexts.height} pertences "
        f"item-contexto, {by_context.height} linhas desnormalizadas por contexto, "
        f"{prematerialized.height} linhas pré-materializadas (base completa)"
    )


if __name__ == "__main__":
    main()
