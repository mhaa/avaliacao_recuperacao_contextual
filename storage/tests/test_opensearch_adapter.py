"""Testes de conformidade do adaptador OpenSearch — exigem um OpenSearch
real com índice criado:

    docker compose up -d opensearch
    docker compose run --rm --entrypoint python tools schemas/opensearch/create_index.py
    docker compose run --rm tools -m integration storage/tests/test_opensearch_adapter.py -v

Cada documento pertence a um único user_id (sem estrutura global
compartilhada, diferente de Valkey/Scylla) — um user_id fora da faixa real
já isola o teste, sem risco de apagar dado de outro usuário.
"""

from __future__ import annotations

import os

import pytest
from opensearchpy import OpenSearch

from storage.opensearch import INDEX, OpenSearchAdapter

pytestmark = pytest.mark.integration

HOSTS = [os.environ.get("TEST_OPENSEARCH_HOST", "http://opensearch:9200")]

_TEST_USER_ID = 999_001


@pytest.fixture
def seeded_user():
    client = OpenSearch(hosts=HOSTS, use_ssl=False, verify_certs=False)
    docs = [
        {"user_id": _TEST_USER_ID, "item_id": 90_000_001, "score": 9.0, "context_ids": [1]},
        {"user_id": _TEST_USER_ID, "item_id": 90_000_002, "score": 8.0, "context_ids": [2]},
        {"user_id": _TEST_USER_ID, "item_id": 90_000_003, "score": 7.0, "context_ids": [1, 2]},
    ]
    for doc in docs:
        client.index(index=INDEX, body=doc, refresh=True)

    yield _TEST_USER_ID

    client.delete_by_query(
        index=INDEX, body={"query": {"term": {"user_id": _TEST_USER_ID}}}, refresh=True
    )


async def test_get_candidates_returns_all_with_context_ids(seeded_user):
    adapter = OpenSearchAdapter(HOSTS)
    result = await adapter.get_candidates(seeded_user)
    by_id = {c.item_id: c for c in result}
    assert set(by_id) == {90_000_001, 90_000_002, 90_000_003}
    assert by_id[90_000_001].context_ids == frozenset({1})
    assert by_id[90_000_003].context_ids == frozenset({1, 2})


async def test_get_candidates_filtered_single_context(seeded_user):
    adapter = OpenSearchAdapter(HOSTS)
    result = await adapter.get_candidates_filtered(seeded_user, [1])
    assert {c.item_id for c in result} == {90_000_001, 90_000_003}


async def test_get_candidates_filtered_is_and_across_contexts(seeded_user):
    adapter = OpenSearchAdapter(HOSTS)
    result = await adapter.get_candidates_filtered(seeded_user, [1, 2])
    assert {c.item_id for c in result} == {90_000_003}


async def test_intersect_single_context(seeded_user):
    adapter = OpenSearchAdapter(HOSTS)
    result = await adapter.intersect(seeded_user, [1], limit=500)
    assert {c.item_id for c in result} == {90_000_001, 90_000_003}


async def test_intersect_composed_context_is_and(seeded_user):
    adapter = OpenSearchAdapter(HOSTS)
    result = await adapter.intersect(seeded_user, [1, 2], limit=500)
    assert {c.item_id for c in result} == {90_000_003}


async def test_unknown_user_returns_empty_list_not_error():
    adapter = OpenSearchAdapter(HOSTS)
    result = await adapter.get_candidates(999_999_999)
    assert result == []
