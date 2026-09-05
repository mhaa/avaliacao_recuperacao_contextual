"""Aplica o esquema do Postgres (BD-1) a partir dos próprios `.sql`
versionados. Localmente isso acontece automaticamente (imagem oficial do
Postgres via docker-entrypoint-initdb.d, ver docker-compose.yml); numa VM
na nuvem (infra/modules/database/) esse mecanismo não existe — Container-
Optimized OS não tem esse diretório de conveniência. Idempotente (os `.sql`
usam `IF NOT EXISTS`), mesmo padrão de schemas/scylla/apply_schema.py.

Uso:
    docker compose up -d postgres
    docker compose run --rm --entrypoint python tools schemas/postgres/apply_schema.py
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

CONNINFO = os.environ.get("TEST_POSTGRES_DSN", "postgresql://tcc:tcc@postgres:5432/recsys")

SCHEMA_DIR = Path(__file__).parent
SQL_FILES = ["001_schema.sql", "002_prematerialized.sql", "003_inverted_lists.sql"]


def main() -> None:
    with psycopg.connect(CONNINFO, autocommit=True) as conn:
        with conn.cursor() as cur:
            for filename in SQL_FILES:
                sql = (SCHEMA_DIR / filename).read_text(encoding="utf-8")
                cur.execute(sql)
    print(f"Esquema Postgres aplicado ({', '.join(SQL_FILES)}).")


if __name__ == "__main__":
    main()
