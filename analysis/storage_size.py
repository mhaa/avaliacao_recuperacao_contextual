"""Volume ocupado por tabela/padrão-de-chave/índice, medido contra a base já
carregada (docs/DESIGN.md, "Custo de armazenamento — a componente que
faltava na dimensão `custo`"). Armazenamento não varia com carga/taxa de
requisição — só precisa ser medido uma vez por tecnologia, não por célula
(`infra/scripts/measure_storage_size.py` orquestra isso).

Cada coletor devolve bytes por tabela/padrão-de-chave/índice, não um total
único de banco: a mesma base compartilhada serve as 3-4 estratégias de uma
tecnologia (`candidates`, `item_contexts`, `prematerialized`,
`inverted_lists` coexistem — `schemas/*/load_full_dataset.py` carrega tudo
de uma vez, sem depender de estratégia), e cada estratégia usa só um
subconjunto dessas estruturas (ver tabela em docs/DESIGN.md) — por isso a
granularidade por tabela/padrão, nunca um número por banco inteiro.

Exato para Postgres/Scylla/OpenSearch (consultas nativas de metadado, sem
estimativa). Valkey é a exceção: sem contabilidade nativa por padrão de
chave, usa amostragem estatística de `MEMORY USAGE` — ver
`valkey_key_pattern_bytes`.

Uso (variáveis de ambiente — mesma convenção de
`infra/scripts/cloud_smoke_test.py:build_storage_env_flags`, nunca um host
por argumento de linha de comando):
    TEST_POSTGRES_DSN=... docker compose run --rm --entrypoint python tools \\
        -m analysis.storage_size postgres
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

import psycopg

# Tabelas por tecnologia relacional/wide-column e padrões de chave/índices —
# ver docs/DESIGN.md pra qual estratégia usa qual. Tabela/padrão/índice
# ausente (schema mais antigo, ou tecnologia sem aquela estratégia) conta 0
# bytes, nunca erro: `candidates_by_context`/`prematerialized` (Scylla) ou
# `inverted_lists` (Postgres, Etapa 4) podem não existir dependendo de qual
# versão de `schemas/*/apply_schema.py` rodou.
POSTGRES_TABLES = ["candidates", "item_contexts", "prematerialized", "inverted_lists"]
SCYLLA_TABLES = ["candidates", "item_contexts", "candidates_by_context", "prematerialized"]
VALKEY_KEY_PATTERNS = [
    "candidates:*",
    "item_contexts:*",
    "candidates_set:*",
    "inverted:*",
    "prematerialized:*",
]
OPENSEARCH_INDICES = ["candidates", "item_contexts"]

# Amostra de chaves por padrão no Valkey — grande o bastante pra estimar
# memória média por chave com folga sobre a variância entre HASHes de
# tamanho variável (prematerialized:* tem até 40 entradas; candidates:*
# sempre 500), pequena o bastante pra não competir com carga real na thread
# única do Valkey durante a medição.
_VALKEY_SAMPLE_SIZE = 2_000


def postgres_table_bytes(conninfo: str, tables: list[str] = POSTGRES_TABLES) -> dict[str, int]:
    """`pg_total_relation_size` por tabela — inclui índices e TOAST, o
    espaço real ocupado em disco, não só as linhas (`pg_relation_size`
    sozinho subestimaria: GIN em `item_contexts`, PK em todas). Tabela
    ausente conta 0, não erro.

    O `to_regclass` roda numa CTE separada, NUNCA como argumento direto de
    `pg_total_relation_size(%s)`: confirmado ao vivo (nuvem, tabela
    genuinamente ausente numa base restaurada de snapshot mais antigo) que
    o Postgres resolve o TIPO de um parâmetro (`regclass`, inferido de
    `pg_total_relation_size`) e converte o valor pra esse tipo NO BIND,
    antes de qualquer `CASE WHEN` rodar — `UndefinedTable` sobe na hora,
    ignorando a proteção. Passando o OID já resolvido pela CTE (nulo se a
    tabela não existir) pra `pg_total_relation_size`, o parâmetro nunca
    precisa ser convertido a partir de um nome de tabela inválido."""
    with psycopg.connect(conninfo, autocommit=True) as conn:
        with conn.cursor() as cur:
            sizes: dict[str, int] = {}
            for table in tables:
                cur.execute(
                    "WITH t AS (SELECT to_regclass(%s) AS oid) "
                    "SELECT CASE WHEN t.oid IS NULL THEN 0 "
                    "ELSE pg_total_relation_size(t.oid) END FROM t",
                    (table,),
                )
                sizes[table] = cur.fetchone()[0]
            return sizes


def scylla_table_bytes(
    hosts: list[str], keyspace: str = "recsys", tables: list[str] = SCYLLA_TABLES
) -> dict[str, int]:
    """`system.size_estimates` — tabela virtual do Cassandra/Scylla com
    `mean_partition_size`/`partitions_count` por (keyspace, tabela, faixa de
    token). Somar todas as faixas de uma tabela dá o tamanho estimado dela —
    nativo do driver CQL, sem SSH nem `nodetool`."""
    from cassandra.cluster import Cluster

    cluster = Cluster(hosts)
    session = cluster.connect()
    try:
        sizes: dict[str, int] = {}
        for table in tables:
            rows = session.execute(
                "SELECT mean_partition_size, partitions_count FROM system.size_estimates "
                "WHERE keyspace_name = %s AND table_name = %s",
                (keyspace, table),
            )
            sizes[table] = sum(
                (row.mean_partition_size or 0) * (row.partitions_count or 0) for row in rows
            )
        return sizes
    finally:
        cluster.shutdown()


def opensearch_index_bytes(
    hosts: list[str], indices: list[str] = OPENSEARCH_INDICES
) -> dict[str, int]:
    """`_cat/indices?bytes=b` — tamanho em disco por índice (store size,
    inclui o índice invertido/postings lists do Lucene, não só os `_source`
    armazenados). Índice ausente devolve 0, não erro — `_cat` simplesmente
    omite índices inexistentes."""
    from opensearchpy import OpenSearch

    client = OpenSearch(hosts=hosts, use_ssl=False, verify_certs=False)
    rows = client.cat.indices(format="json", bytes="b")
    stats = {row["index"]: int(row["store.size"]) for row in rows}
    return {index: stats.get(index, 0) for index in indices}


def valkey_key_pattern_bytes(
    url: str,
    patterns: list[str] = VALKEY_KEY_PATTERNS,
    sample_size: int = _VALKEY_SAMPLE_SIZE,
    seed: int | None = None,
) -> dict[str, dict[str, float | int]]:
    """Único dos 4 coletores que estima em vez de medir exato: Valkey não
    tem contabilidade nativa de memória por padrão de chave (`INFO memory`
    só dá o total do processo inteiro). Por padrão: `SCAN` conta as chaves
    de verdade (exato, via `scan_iter`) e amostra até `sample_size` delas
    para `MEMORY USAGE` (bytes reais por chave amostrada, incluindo overhead
    de estrutura do Valkey) — `bytes_estimate = key_count × média_amostral`.
    Devolve `key_count`/`sampled`/`bytes_estimate` por padrão, para a margem
    de erro ficar auditável junto ao resultado, não escondida atrás de um
    único número."""
    import valkey as valkey_lib

    client = valkey_lib.Valkey.from_url(url, decode_responses=True)
    rng = random.Random(seed)
    result: dict[str, dict[str, float | int]] = {}
    for pattern in patterns:
        keys = list(client.scan_iter(match=pattern, count=1000))
        key_count = len(keys)
        sample = rng.sample(keys, min(sample_size, key_count)) if key_count else []
        if sample:
            usages = [client.memory_usage(key) or 0 for key in sample]
            mean_bytes = sum(usages) / len(usages)
        else:
            mean_bytes = 0.0
        result[pattern] = {
            "key_count": key_count,
            "sampled": len(sample),
            "bytes_estimate": round(mean_bytes * key_count),
        }
    return result


def postgres_storage_bytes(conninfo: str, dbname: str) -> int:
    """Tamanho do banco inteiro (`pg_database_size`) — mantido por
    compatibilidade com o único uso anterior deste módulo. `main()`/o custo
    por estratégia usam `postgres_table_bytes`, não esta função."""
    with psycopg.connect(conninfo, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_database_size(%s)", (dbname,))
            return cur.fetchone()[0]


STORAGE_SIZE_COLLECTORS = {
    "postgres": postgres_storage_bytes,
}


def write_storage_json(storage_bytes: int, backend: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"backend": backend, "storage_bytes": storage_bytes}, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("storage", choices=["postgres", "valkey", "scylla", "opensearch"])
    args = parser.parse_args(argv)

    if args.storage == "postgres":
        conninfo = os.environ.get("TEST_POSTGRES_DSN", "postgresql://tcc:tcc@postgres:5432/recsys")
        sizes = postgres_table_bytes(conninfo)
    elif args.storage == "scylla":
        hosts = os.environ.get("TEST_SCYLLA_HOSTS", "scylla").split(",")
        sizes = scylla_table_bytes(hosts)
    elif args.storage == "opensearch":
        hosts = [os.environ.get("TEST_OPENSEARCH_HOST", "http://opensearch:9200")]
        sizes = opensearch_index_bytes(hosts)
    else:
        url = os.environ.get("TEST_VALKEY_URL", "redis://valkey:6379/0")
        sizes = valkey_key_pattern_bytes(url)

    print(f"STORAGE_RESULT {json.dumps({'backend': args.storage, 'sizes': sizes})}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
