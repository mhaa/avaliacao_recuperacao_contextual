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
# Linhas por chamada de execute_concurrent_with_args — ver
# _execute_concurrent_batched abaixo para o motivo real (não é só limitar
# concorrência, é limitar o que o driver materializa em memória).
_CONCURRENT_ARGS_BATCH_SIZE = 20_000


def _chunked(rows: list, size: int):
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _execute_concurrent_batched(
    session, statement, rows_iter, concurrency: int, label: str = ""
) -> None:
    """`execute_concurrent_with_args()` do driver Cassandra/Scylla NÃO faz
    streaming de verdade, apesar de aceitar um iterador como `parameters` —
    a implementação do driver materializa TODOS os parâmetros numa lista
    (`list(enumerate(...))`) antes de despachar qualquer requisição,
    independente do valor de `concurrency` (que só limita quantas
    requisições ficam em voo ao mesmo tempo, não o que já foi carregado em
    memória). Em escala real (~100,5M linhas de candidates) isso mata o
    processo por OOM — confirmado ao vivo rodando e1-scylla pela primeira
    vez, mesmo depois do fix já aplicado em candidates_by_context (que usa
    execute_concurrent, não execute_concurrent_with_args, mas tinha o mesmo
    problema de acumular tudo antes de executar). Chamar
    execute_concurrent_with_args repetidamente em fatias pequenas do
    próprio iterador mantém a memória limitada, independente do tamanho
    total do dataset.

    `label`, quando dado, imprime uma linha de progresso a cada fatia —
    sem isso, a carga inteira (~horas em escala real) roda muda até o
    único print no fim de main(). Confirmado ao vivo: sem heartbeat
    nenhum, uma sessão SSH que trava no meio fica indistinguível de uma
    que só está demorando, e ninguém percebe até horas depois."""
    batch: list = []
    total = 0
    for row in rows_iter:
        batch.append(row)
        if len(batch) >= _CONCURRENT_ARGS_BATCH_SIZE:
            execute_concurrent_with_args(session, statement, batch, concurrency=concurrency)
            total += len(batch)
            if label:
                print(f"{label}: {total} linhas gravadas", flush=True)
            batch = []
    if batch:
        execute_concurrent_with_args(session, statement, batch, concurrency=concurrency)
        total += len(batch)
        if label:
            print(f"{label}: {total} linhas gravadas (final)", flush=True)


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
    _execute_concurrent_batched(
        session,
        insert_candidates,
        candidates.select(["user_id", "rank", "item_id", "score"]).iter_rows(),
        concurrency=20,
        label="candidates",
    )

    insert_item_contexts = session.prepare(
        "INSERT INTO item_contexts (item_id, context_id) VALUES (?, ?)"
    )
    _execute_concurrent_batched(
        session,
        insert_item_contexts,
        item_contexts.iter_rows(),
        concurrency=100,
        label="item_contexts",
    )

    by_context = candidates.select(["user_id", "item_id", "rank", "score"]).join(
        item_contexts, on="item_id", how="inner"
    )
    insert_by_context = session.prepare(
        "INSERT INTO candidates_by_context (context_id, user_id, rank, item_id, score) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    # Flush a cada _BATCHES_FLUSH_SIZE BatchStatements, não só no final —
    # `by_context` é um join (candidates x item_contexts), maior ainda que
    # candidates sozinho em escala real; acumular TODOS os BatchStatement
    # antes de um único execute_concurrent() no final corre o mesmo risco de
    # OOM já confirmado ao vivo em schemas/valkey/load_full_dataset.py
    # (pipeline inteiro em memória antes de mandar qualquer coisa pro
    # servidor) — correção proativa, nunca chegou a estourar aqui de
    # verdade, mas é a mesma causa.
    _BATCHES_FLUSH_SIZE = 2000
    batches: list = []
    context_rows_done = 0
    for (context_id, user_id), group in by_context.group_by(["context_id", "user_id"]):
        rows = group.select(["rank", "item_id", "score"]).rows()
        for chunk in _chunked(rows, _BATCH_CHUNK_SIZE):
            batch = BatchStatement(batch_type=BatchType.UNLOGGED)
            for rank, item_id, score in chunk:
                batch.add(insert_by_context, (context_id, user_id, rank, item_id, score))
            batches.append((batch, None))
            context_rows_done += len(chunk)
            if len(batches) >= _BATCHES_FLUSH_SIZE:
                execute_concurrent(session, batches, concurrency=10)
                # Heartbeat: candidates_by_context é o maior dos 4 (join
                # com item_contexts) e, sem isso, é a fase mais longa sem
                # nenhuma linha impressa — mesmo motivo do `label` em
                # _execute_concurrent_batched acima.
                print(f"candidates_by_context: ~{context_rows_done} linhas gravadas", flush=True)
                batches = []
    if batches:
        execute_concurrent(session, batches, concurrency=10)
        print(f"candidates_by_context: ~{context_rows_done} linhas gravadas (final)", flush=True)

    insert_prematerialized = session.prepare(
        "INSERT INTO prematerialized (user_id, context_id, rank, item_id, score) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    _execute_concurrent_batched(
        session,
        insert_prematerialized,
        prematerialized.select(
            ["user_id", "context_id", "rank", "item_id", "score"]
        ).iter_rows(),
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
