"""storage.json — volume ocupado em disco, medido após a carga
(IMPLEMENTACAO.md, "Coleta de resultados"). Uma consulta por tecnologia de
banco: diferente de resources.csv, um relacional, um KV, um wide-column e
um índice invertido não compartilham primitiva nenhuma de "tamanho em
disco", então isto não força uma interface comum onde não existe uma.

Só Postgres está implementado por enquanto — os outros três (Valkey via
`INFO memory`, Scylla via `nodetool status`/tamanho do sstable, OpenSearch
via `_cat/indices?bytes`) ficam para quando a Etapa 9 (infra/) existir e
houver uma instância de verdade para validar contra, em vez de adivinhar o
formato de saída de cada ferramenta sem poder rodar."""

from __future__ import annotations

import json
from pathlib import Path

import psycopg


def postgres_storage_bytes(conninfo: str, dbname: str) -> int:
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
