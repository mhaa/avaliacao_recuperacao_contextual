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
# Fora da faixa real de contextos (C=20, ids 1..20) — mesmo cuidado do
# docstring do módulo, agora estendido a `inverted_lists`: sua PK é só
# `context_id`, então um id real colidiria com uma lista invertida global
# de verdade, carregada por load_oracle_fixture.py.
_TEST_CONTEXT_IDS = [9001, 9002]


def _seed():
    with psycopg.connect(CONNINFO, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM candidates WHERE user_id = %s", (_TEST_USER_ID,))
            cur.execute("DELETE FROM item_contexts WHERE item_id = ANY(%s)", (_TEST_ITEM_IDS,))
            cur.execute(
                "DELETE FROM inverted_lists WHERE context_id = ANY(%s)", (_TEST_CONTEXT_IDS,)
            )
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
            # Lista invertida de teste: contexto 9001 = {item0, item2},
            # contexto 9002 = {item1, item2} — item2 é o único presente nos
            # dois, mesma forma dos dados de get_candidates_filtered acima,
            # para exercitar a semântica AND de intersect.
            cur.executemany(
                "INSERT INTO inverted_lists (context_id, item_ids) VALUES (%s, %s)",
                [
                    (_TEST_CONTEXT_IDS[0], [_TEST_ITEM_IDS[0], _TEST_ITEM_IDS[2]]),
                    (_TEST_CONTEXT_IDS[1], [_TEST_ITEM_IDS[1], _TEST_ITEM_IDS[2]]),
                ],
            )


def _cleanup():
    with psycopg.connect(CONNINFO, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM candidates WHERE user_id = %s", (_TEST_USER_ID,))
            cur.execute("DELETE FROM item_contexts WHERE item_id = ANY(%s)", (_TEST_ITEM_IDS,))
            cur.execute(
                "DELETE FROM inverted_lists WHERE context_id = ANY(%s)", (_TEST_CONTEXT_IDS,)
            )


@pytest.fixture
def seeded_user():
    _seed()
    yield _TEST_USER_ID
    _cleanup()


@pytest.fixture
async def adapter():
    # Fecha o pool ao final de cada teste: pytest-asyncio dá um event loop
    # novo por função de teste (function scope), e um AsyncConnectionPool
    # aberto num loop que já fechou trava o próximo teste indefinidamente
    # (workers de fundo do pool anterior ficam presos ao loop morto) —
    # confirmado travando de verdade antes deste fixture existir.
    adapter = PostgresAdapter(CONNINFO)
    yield adapter
    await adapter.close()


async def test_get_candidates_ordered_by_rank(adapter, seeded_user):
    result = await adapter.get_candidates(seeded_user)
    assert [c.item_id for c in result] == _TEST_ITEM_IDS


async def test_get_candidates_returns_all_items(adapter, seeded_user):
    # Pertença item->contexto não viaja mais em Candidate (Fase 2.6,
    # catálogo em memória — core/contract.py:Candidate só tem
    # item_id/score); essa cobertura mora em strategies/tests/, não aqui.
    result = await adapter.get_candidates(seeded_user)
    assert {c.item_id for c in result} == set(_TEST_ITEM_IDS)


async def test_get_candidates_filtered_returns_only_matching_context(adapter, seeded_user):
    result = await adapter.get_candidates_filtered(seeded_user, [1])
    assert {c.item_id for c in result} == {_TEST_ITEM_IDS[0], _TEST_ITEM_IDS[2]}


async def test_get_candidates_filtered_is_and_across_contexts(adapter, seeded_user):
    result = await adapter.get_candidates_filtered(seeded_user, [1, 2])
    assert {c.item_id for c in result} == {_TEST_ITEM_IDS[2]}


async def test_get_candidates_filtered_empty_context_returns_everything(adapter, seeded_user):
    result = await adapter.get_candidates_filtered(seeded_user, [])
    assert {c.item_id for c in result} == set(_TEST_ITEM_IDS)


async def test_unknown_user_returns_empty_list_not_error(adapter):
    result = await adapter.get_candidates(999_999_999)
    assert result == []


async def test_intersect_single_context(adapter, seeded_user):
    result = await adapter.intersect(seeded_user, [_TEST_CONTEXT_IDS[0]], limit=500)
    assert {c.item_id for c in result} == {_TEST_ITEM_IDS[0], _TEST_ITEM_IDS[2]}


async def test_intersect_is_and_across_contexts(adapter, seeded_user):
    result = await adapter.intersect(seeded_user, _TEST_CONTEXT_IDS, limit=500)
    assert {c.item_id for c in result} == {_TEST_ITEM_IDS[2]}


async def test_intersect_empty_context_returns_nothing(adapter, seeded_user):
    # Diferente de get_candidates_filtered([]) (devolve tudo): E-4 não tem
    # lista invertida global para intersectar sem contexto, mesmo contrato
    # de storage/valkey.py e storage/opensearch.py.
    result = await adapter.intersect(seeded_user, [], limit=500)
    assert result == []


async def test_intersect_respects_limit(adapter, seeded_user):
    result = await adapter.intersect(seeded_user, [_TEST_CONTEXT_IDS[0]], limit=1)
    assert len(result) == 1
    assert {c.item_id for c in result} <= {_TEST_ITEM_IDS[0], _TEST_ITEM_IDS[2]}
