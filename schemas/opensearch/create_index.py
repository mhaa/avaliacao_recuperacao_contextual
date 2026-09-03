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
# Índice de catálogo, separado do de candidatos: ~87.585 documentos (um por
# item), lido em massa UMA vez na montagem da célula
# (storage/opensearch.py:load_item_contexts) para o catálogo em memória de
# core/catalog.py. O `context_ids` desnormalizado dentro de cada documento
# de `candidates` continua existindo — é o que a query `term` de E-2 usa —
# mas varrer ~100M documentos para reconstruir a pertença item->contexto
# seria absurdo; daí o índice próprio. Custo: alguns MB.
CATALOG_INDEX = "item_contexts"

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


# 1 shard: ~87.585 documentos minúsculos, lidos em massa uma única vez.
# Fragmentar não compra paralelismo útil e só multiplica overhead por busca.
_CATALOG_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
    },
    "mappings": {
        "properties": {
            "item_id": {"type": "integer"},
            "context_ids": {"type": "integer"},
        }
    },
}


def main() -> None:
    client = OpenSearch(hosts=HOSTS, use_ssl=False, verify_certs=False)
    for index, mapping in ((INDEX, _MAPPING), (CATALOG_INDEX, _CATALOG_MAPPING)):
        if client.indices.exists(index=index):
            print(f"Índice '{index}' já existe.")
            continue
        client.indices.create(index=index, body=mapping)
        print(f"Índice '{index}' criado.")


if __name__ == "__main__":
    main()
