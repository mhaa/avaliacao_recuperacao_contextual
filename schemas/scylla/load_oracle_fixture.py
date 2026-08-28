"""Carrega, em um ScyllaDB já com o esquema aplicado, os dados de
harness/fixtures.py necessários para verificar os 1000 casos do oráculo.
Ver storage/scylla.py para o esquema de tabelas.

Uso:
    docker compose up -d scylla
    docker compose run --rm --entrypoint python tools schemas/scylla/apply_schema.py
    docker compose run --rm --entrypoint python tools schemas/scylla/load_oracle_fixture.py
"""

from __future__ import annotations

import os

from cassandra.cluster import Cluster
from cassandra.concurrent import execute_concurrent, execute_concurrent_with_args
from cassandra.query import BatchStatement, BatchType

from harness import fixtures

HOSTS = os.environ.get("TEST_SCYLLA_HOSTS", "scylla").split(",")
KEYSPACE = "recsys"

# Linhas por lote dentro de uma mesma partição — mantém o tamanho do BATCH
# bem abaixo do limiar de aviso do Scylla mesmo para os contextos mais
# amplos (ex. "Drama", ~46% de seletividade — ver data_generation/README.md).
_BATCH_CHUNK_SIZE = 100


def _chunked(rows: list, size: int):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def main() -> None:
    cluster = Cluster(HOSTS)
    session = cluster.connect(KEYSPACE)
    # Cluster de desenvolvimento de 1 nó com --smp 1 (ver docker-compose.yml)
    # — concorrência/timeout default do driver são conservadores demais
    # para o volume de linhas desta carga.
    session.default_timeout = 60.0

    for table in ("candidates", "item_contexts", "candidates_by_context", "prematerialized"):
        session.execute(f"TRUNCATE {table}")

    user_ids = fixtures.needed_user_ids()
    candidates = fixtures.load_candidates(user_ids)
    item_contexts = fixtures.load_item_contexts()
    prematerialized = fixtures.load_prematerialized(user_ids)

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

    # candidates_by_context é desnormalizada: uma linha por (usuário, item,
    # contexto ao qual o item pertence) — junta candidates com item_contexts
    # em memória (polars join), já que CQL não tem join.
    by_context = candidates.select(["user_id", "item_id", "rank", "score"]).join(
        item_contexts, on="item_id", how="inner"
    )
    insert_by_context = session.prepare(
        "INSERT INTO candidates_by_context (context_id, user_id, rank, item_id, score) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    # ~2x mais linhas que as outras tabelas (uma por item x contexto do
    # item). Inserir uma linha de cada vez (mesmo concorrente) satura o
    # único shard do cluster de desenvolvimento (--smp 1) — ver histórico de
    # WriteTimeout ao implementar esta etapa. Em vez disso, agrupa por
    # partição (context_id, user_id) e escreve cada uma como um BATCH
    # UNLOGGED: cai de ~850 mil requisições para uma por partição (milhares,
    # não centenas de milhares).
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
        f"{prematerialized.height} linhas pré-materializadas, {len(user_ids)} usuários "
        "referenciados pelo oráculo"
    )


if __name__ == "__main__":
    main()
