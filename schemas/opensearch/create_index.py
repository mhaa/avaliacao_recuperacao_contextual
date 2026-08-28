"""Cria o índice 'candidates' do OpenSearch (BD-4). Idempotente.

Uso:
    docker compose up -d opensearch
    docker compose run --rm --entrypoint python tools schemas/opensearch/create_index.py
"""

from __future__ import annotations

import os

from opensearchpy import OpenSearch

HOSTS = [os.environ.get("TEST_OPENSEARCH_HOST", "http://opensearch:9200")]
INDEX = "candidates"

_MAPPING = {
    "mappings": {
        "properties": {
            "user_id": {"type": "integer"},
            "item_id": {"type": "integer"},
            "score": {"type": "float"},
            "context_ids": {"type": "integer"},
        }
    }
}


def main() -> None:
    client = OpenSearch(hosts=HOSTS, use_ssl=False, verify_certs=False)
    if client.indices.exists(index=INDEX):
        print(f"Índice '{INDEX}' já existe.")
        return
    client.indices.create(index=INDEX, body=_MAPPING)
    print(f"Índice '{INDEX}' criado.")


if __name__ == "__main__":
    main()
