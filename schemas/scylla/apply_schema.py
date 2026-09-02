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

# LeveledCompactionStrategy, não o default SizeTieredCompactionStrategy:
# estas tabelas são carregadas uma vez (schemas/scylla/load_full_dataset.py)
# e só lidas depois. STCS otimiza escrita e deixa o mesmo dado espalhado em
# várias SSTables, custando mais leituras por consulta; LCS é a recomendação
# para carga write-once/read-many. Não muda semântica nenhuma — é layout em
# disco, igual para todas as estratégias.
_COMPACTION = "{'class': 'LeveledCompactionStrategy'}"

_TABLES = ["candidates", "item_contexts", "candidates_by_context", "prematerialized"]

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
    ) WITH compaction = {_COMPACTION}
    """,
    f"""
    CREATE TABLE IF NOT EXISTS {KEYSPACE}.item_contexts (
        item_id int,
        context_id smallint,
        PRIMARY KEY (item_id, context_id)
    ) WITH compaction = {_COMPACTION}
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
    ) WITH compaction = {_COMPACTION}
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
    ) WITH compaction = {_COMPACTION}
    """,
]


def main() -> None:
    cluster = Cluster(HOSTS)
    session = cluster.connect()
    for statement in _STATEMENTS:
        session.execute(statement)
    # ALTER separado do CREATE: `CREATE TABLE IF NOT EXISTS` é no-op numa
    # tabela que já existe, então sem isto uma base criada antes desta
    # mudança continuaria em STCS para sempre. ALTER é idempotente — rodar
    # de novo numa tabela já em LCS não faz nada.
    for table in _TABLES:
        session.execute(
            f"ALTER TABLE {KEYSPACE}.{table} WITH compaction = {_COMPACTION}"
        )
    cluster.shutdown()
    print(f"Esquema Scylla aplicado (compaction: LCS em {len(_TABLES)} tabelas).")


if __name__ == "__main__":
    main()
