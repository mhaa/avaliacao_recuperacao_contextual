"""Cria o keyspace e as tabelas do ScyllaDB (BD-3). Idempotente
(`IF NOT EXISTS`) — Scylla não tem um mecanismo tipo
docker-entrypoint-initdb.d do Postgres, então isso roda como um passo
manual.

Uso:
    docker compose up -d scylla
    docker compose run --rm --entrypoint python tools schemas/scylla/apply_schema.py

Modelagem (sem joins, sem filtro arbitrário fora da chave de partição/
clustering — CQL não tem isso): ver docstring de storage/scylla.py.
"""

from __future__ import annotations

import os

from cassandra.cluster import Cluster

HOSTS = os.environ.get("TEST_SCYLLA_HOSTS", "scylla").split(",")
KEYSPACE = "recsys"

_STATEMENTS = [
    f"""
    CREATE KEYSPACE IF NOT EXISTS {KEYSPACE}
    WITH replication = {{'class': 'SimpleStrategy', 'replication_factor': 1}}
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {KEYSPACE}.candidates (
        user_id int,
        rank smallint,
        item_id int,
        score float,
        PRIMARY KEY (user_id, rank)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {KEYSPACE}.item_contexts (
        item_id int,
        context_id smallint,
        PRIMARY KEY (item_id, context_id)
    )
    """,
    # Desnormalizada, partição por (contexto, usuário) — leitura direta e já
    # filtrada por um único contexto (E-2). Não truncada, então intersectar
    # múltiplas leituras (contexto composto) em Python é correto.
    f"""
    CREATE TABLE IF NOT EXISTS {KEYSPACE}.candidates_by_context (
        context_id smallint,
        user_id int,
        rank smallint,
        item_id int,
        score float,
        PRIMARY KEY ((context_id, user_id), rank)
    )
    """,
    # Chave composta (usuário, contexto) — leitura direta para E-3.
    f"""
    CREATE TABLE IF NOT EXISTS {KEYSPACE}.prematerialized (
        user_id int,
        context_id smallint,
        rank smallint,
        item_id int,
        score float,
        PRIMARY KEY ((user_id, context_id), rank)
    )
    """,
]


def main() -> None:
    cluster = Cluster(HOSTS)
    session = cluster.connect()
    for statement in _STATEMENTS:
        session.execute(statement)
    cluster.shutdown()
    print("Esquema Scylla aplicado.")


if __name__ == "__main__":
    main()
