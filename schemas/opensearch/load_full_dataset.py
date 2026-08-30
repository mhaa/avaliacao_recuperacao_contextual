"""Carrega, em um OpenSearch já com o índice criado, a base completa (todos
os usuários, não só o subconjunto do oráculo — ver
schemas/opensearch/load_oracle_fixture.py para esse) — usado pela bateria
de medição real da Fase 5 (infra/scripts/run_measurement_battery.py),
nunca pelo smoke test.

Uso:
    docker compose run --rm --entrypoint python tools schemas/opensearch/load_full_dataset.py
"""

from __future__ import annotations

import os

from opensearchpy import OpenSearch
from opensearchpy.helpers import bulk

from harness import fixtures

HOSTS = [os.environ.get("TEST_OPENSEARCH_HOST", "http://opensearch:9200")]
INDEX = "candidates"


def main() -> None:
    fixtures.ensure_full_dataset_downloaded()

    client = OpenSearch(hosts=HOSTS, use_ssl=False, verify_certs=False)
    client.delete_by_query(
        index=INDEX, body={"query": {"match_all": {}}}, conflicts="proceed", refresh=True
    )

    candidates = fixtures.load_candidates()
    item_contexts = fixtures.load_item_contexts()

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

    print(f"Carregado: {success} documentos indexados (base completa)")


if __name__ == "__main__":
    main()
