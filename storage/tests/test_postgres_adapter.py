"""Testes de conformidade do adaptador Postgres — exigem um Postgres real:

    docker compose up -d postgres
    docker compose run --rm tools -m integration storage/tests/test_postgres_adapter.py -v

`TEST_POSTGRES_DSN` sobrescreve a string de conexão (default: hostname do
serviço `postgres` do docker-compose.yml). Usa user_id E item_id fora da
faixa real de dados (catálogo real é denso, 0..87584 — ver
data_generation/README.md) para nunca colidir com massa carregada por
schemas/postgres/load_oracle_fixture.py: uma colisão de item_id faria o
teardown (DELETE por item_id) apagar pertences item->contexto reais, sem
erro visível, só corrompendo silenciosamente os resultados de outro teste
rodado depois na mesma sessão de banco.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from storage.postgres import PostgresAdapter

pytestmark = pytest.mark.integration

CONNINFO = os.environ.get("TEST_POSTGRES_DSN", "postgresql://tcc:tcc@postgres:5432/recsys")

_TEST_USER_ID = 999_001
_TEST_ITEM_IDS = [90_000_001, 90_000_002, 90_000_003]


def _seed():
    with psycopg.connect(CONNINFO, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM candidates WHERE user_id = %s", (_TEST_USER_ID,))
            cur.execute("DELETE FROM item_contexts WHERE item_id = ANY(%s)", (_TEST_ITEM_IDS,))
            cur.executemany(
                "INSERT INTO candidates (user_id, item_id, rank, score) VALUES (%s, %s, %s, %s)",
                [
                    (_TEST_USER_ID, _TEST_ITEM_IDS[0], 1, 9.0),
                    (_TEST_USER_ID, _TEST_ITEM_IDS[1], 2, 8.0),
                    (_TEST_USER_ID, _TEST_ITEM_IDS[2], 3, 7.0),
                ],
            )
            cur.executemany(
                "INSERT INTO item_contexts (item_id, context_id) VALUES (%s, %s)",
                [
                    (_TEST_ITEM_IDS[0], 1),
                    (_TEST_ITEM_IDS[1], 2),
                    (_TEST_ITEM_IDS[2], 1),
                    (_TEST_ITEM_IDS[2], 2),
                ],
            )


def _cleanup():
    with psycopg.connect(CONNINFO, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM candidates WHERE user_id = %s", (_TEST_USER_ID,))
            cur.execute("DELETE FROM item_contexts WHERE item_id = ANY(%s)", (_TEST_ITEM_IDS,))


@pytest.fixture
def seeded_user():
    _seed()
    yield _TEST_USER_ID
    _cleanup()


async def test_get_candidates_ordered_by_rank(seeded_user):
    adapter = PostgresAdapter(CONNINFO)
    result = await adapter.get_candidates(seeded_user)
    assert [c.item_id for c in result] == _TEST_ITEM_IDS


async def test_get_candidates_carries_context_membership(seeded_user):
    adapter = PostgresAdapter(CONNINFO)
    result = await adapter.get_candidates(seeded_user)
    context_ids_by_item = {c.item_id: c.context_ids for c in result}
    assert context_ids_by_item[_TEST_ITEM_IDS[0]] == frozenset({1})
    assert context_ids_by_item[_TEST_ITEM_IDS[1]] == frozenset({2})
    assert context_ids_by_item[_TEST_ITEM_IDS[2]] == frozenset({1, 2})


async def test_get_candidates_filtered_returns_only_matching_context(seeded_user):
    adapter = PostgresAdapter(CONNINFO)
    result = await adapter.get_candidates_filtered(seeded_user, [1])
    assert {c.item_id for c in result} == {_TEST_ITEM_IDS[0], _TEST_ITEM_IDS[2]}


async def test_get_candidates_filtered_is_and_across_contexts(seeded_user):
    adapter = PostgresAdapter(CONNINFO)
    result = await adapter.get_candidates_filtered(seeded_user, [1, 2])
    assert {c.item_id for c in result} == {_TEST_ITEM_IDS[2]}


async def test_get_candidates_filtered_empty_context_returns_everything(seeded_user):
    adapter = PostgresAdapter(CONNINFO)
    result = await adapter.get_candidates_filtered(seeded_user, [])
    assert {c.item_id for c in result} == set(_TEST_ITEM_IDS)


async def test_unknown_user_returns_empty_list_not_error():
    adapter = PostgresAdapter(CONNINFO)
    result = await adapter.get_candidates(999_999_999)
    assert result == []
