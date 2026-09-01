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
    # number_of_shards é imutável após a criação (só muda via reindex) —
    # por isso decidido aqui, não como um ajuste de runtime do loader. 4
    # shards para a VM de nuvem (n2-standard-8, 8 vCPUs): 1 shard só
    # concentraria toda escrita/busca num único índice Lucene, deixando a
    # maior parte dos vCPUs ociosa durante a carga (mesmo efeito prático
    # de um `concurrency=1` no lado do servidor). number_of_replicas=0:
    # cluster de nó único (discovery.type=single-node, local e nuvem) nunca
    # aloca a réplica mesmo com o default de 1 — deixa explícito e evita o
    # health "yellow" permanente sem custo real (dado descartável, recarga
    # é o próprio load_full_dataset.py).
    "settings": {
        "number_of_shards": 4,
        "number_of_replicas": 0,
    },
    "mappings": {
        "properties": {
            "user_id": {"type": "integer"},
            "item_id": {"type": "integer"},
            "score": {"type": "float"},
            "context_ids": {"type": "integer"},
        }
    },
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
