"""Carrega, em um OpenSearch já com o índice criado, os dados de
harness/fixtures.py necessários para verificar os 1000 casos do oráculo.

Uso:
    docker compose up -d opensearch
    docker compose run --rm --entrypoint python tools schemas/opensearch/create_index.py
    docker compose run --rm --entrypoint python tools schemas/opensearch/load_oracle_fixture.py
"""

from __future__ import annotations

import os

from opensearchpy import OpenSearch
from opensearchpy.helpers import bulk

from harness import fixtures

HOSTS = [os.environ.get("TEST_OPENSEARCH_HOST", "http://opensearch:9200")]
INDEX = "candidates"


def main() -> None:
    client = OpenSearch(hosts=HOSTS, use_ssl=False, verify_certs=False)
    client.delete_by_query(
        index=INDEX, body={"query": {"match_all": {}}}, conflicts="proceed", refresh=True
    )

    user_ids = fixtures.needed_user_ids()
    candidates = fixtures.load_candidates(user_ids)
    item_contexts = fixtures.load_item_contexts()

    # Pequeno o bastante (166 mil linhas) para agregar em memória: dict
    # item_id -> [context_ids], usado para anexar context_ids a cada
    # documento de candidato na hora da indexação.
    context_ids_by_item: dict[int, list[int]] = {}
    for row in item_contexts.iter_rows(named=True):
        context_ids_by_item.setdefault(row["item_id"], []).append(row["context_id"])

    def _actions():
        for row in candidates.iter_rows(named=True):
            yield {
                "_index": INDEX,
                "_source": {
                    "user_id": row["user_id"],
                    "item_id": row["item_id"],
                    "score": row["score"],
                    "context_ids": context_ids_by_item.get(row["item_id"], []),
                },
            }

    success, _errors = bulk(client, _actions(), chunk_size=2000)
    client.indices.refresh(index=INDEX)

    print(
        f"Carregado: {success} documentos indexados, {len(user_ids)} usuários "
        "referenciados pelo oráculo"
    )


if __name__ == "__main__":
    main()
