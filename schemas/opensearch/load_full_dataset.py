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
from opensearchpy.helpers import bulk, parallel_bulk

from harness import fixtures

HOSTS = [os.environ.get("TEST_OPENSEARCH_HOST", "http://opensearch:9200")]
INDEX = "candidates"
CATALOG_INDEX = "item_contexts"


def _catalog_actions(context_ids_by_item: dict[int, list[int]]):
    """Documentos do índice de catálogo (um por item), com _id = item_id
    para a recarga ser idempotente sem duplicar."""
    for item_id, context_ids in context_ids_by_item.items():
        yield {
            "_index": CATALOG_INDEX,
            "_id": str(item_id),
            "_source": {"item_id": item_id, "context_ids": context_ids},
        }


# Lotes em paralelo via parallel_bulk (thread pool no cliente), não bulk()
# sequencial — bulk() espera a resposta de um lote antes de mandar o
# próximo, deixando o threadpool `write` do OpenSearch (dimensionado para
# os 8 vCPUs da VM de nuvem, n2-standard-8) majoritariamente ocioso.
# Default 8 casa com esse threadpool; ajustável sem alterar código, mesmo
# padrão do TEST_SCYLLA_LOAD_CONCURRENCY.
_THREAD_COUNT = int(os.environ.get("TEST_OPENSEARCH_LOAD_THREADS", "8"))
_CHUNK_SIZE = 2000


def main() -> None:
    fixtures.ensure_full_dataset_downloaded()

    # timeout maior que o default de 10s: lotes concorrentes (parallel_bulk)
    # competem pelo mesmo threadpool `write` do servidor, então um lote
    # individual pode legitimamente demorar mais sob carga real do que em
    # execução sequencial sem que isso indique um problema.
    client = OpenSearch(hosts=HOSTS, use_ssl=False, verify_certs=False, timeout=60)
    client.delete_by_query(
        index=INDEX, body={"query": {"match_all": {}}}, conflicts="proceed", refresh=True
    )
    client.delete_by_query(
        index=CATALOG_INDEX, body={"query": {"match_all": {}}}, conflicts="proceed", refresh=True
    )

    # Desliga refresh automático (default 1s) durante a carga — cada
    # refresh cria um segmento Lucene novo pesquisável, custo real
    # multiplicado por hora de carga em ~100M documentos. Reativado no
    # default depois, com um refresh explícito (já existia antes).
    client.indices.put_settings(index=INDEX, body={"index": {"refresh_interval": "-1"}})

    candidates = fixtures.load_candidates()
    item_contexts = fixtures.load_item_contexts()

    context_ids_by_item: dict[int, list[int]] = {}
    for row in item_contexts.iter_rows(named=True):
        context_ids_by_item.setdefault(row["item_id"], []).append(row["context_id"])

    def _actions():
        # Print periódico por documento enviado — sem isso, parallel_bulk()
        # consome o gerador inteiro em silêncio até acabar (mesmo raciocínio de
        # schemas/scylla/load_full_dataset.py: só o print no fim de main()
        # não dá nenhum sinal de vida durante uma carga real de dezenas de
        # milhões de documentos). Contagem de envio, não de indexação
        # confirmada — aproximação suficiente para heartbeat.
        sent = 0
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
            sent += 1
            if sent % 500_000 == 0:
                print(f"candidates: {sent} documentos enviados para indexação", flush=True)
        print(f"candidates: {sent} documentos enviados para indexação (final)", flush=True)

    success = 0
    for ok, _info in parallel_bulk(
        client, _actions(), chunk_size=_CHUNK_SIZE, thread_count=_THREAD_COUNT
    ):
        if ok:
            success += 1

    # Catálogo: bulk() sequencial basta — ~87.585 documentos minúsculos,
    # irrelevante perto dos ~100M de candidatos acima.
    catalog_success, _catalog_errors = bulk(
        client, _catalog_actions(context_ids_by_item), chunk_size=_CHUNK_SIZE
    )

    client.indices.put_settings(index=INDEX, body={"index": {"refresh_interval": "1s"}})
    client.indices.refresh(index=INDEX)
    client.indices.refresh(index=CATALOG_INDEX)

    print(
        f"Carregado: {success} documentos indexados, {catalog_success} itens de "
        "catálogo (base completa)"
    )


if __name__ == "__main__":
    main()
