"""Testes de conformidade do adaptador ScyllaDB — exigem um Scylla real com
esquema aplicado:

    docker compose up -d scylla
    docker compose run --rm --entrypoint python tools schemas/scylla/apply_schema.py
    docker compose run --rm tools -m integration storage/tests/test_scylla_adapter.py -v

Usa user_id E item_id/context_id fora da faixa real (mesmo cuidado das
outras suítes de adaptador desta etapa).
"""

from __future__ import annotations

import os

import pytest
from cassandra.cluster import Cluster

from storage.scylla import KEYSPACE, ScyllaAdapter

pytestmark = pytest.mark.integration

HOSTS = os.environ.get("TEST_SCYLLA_HOSTS", "scylla").split(",")

_TEST_USER_ID = 999_001
_ITEM_A, _ITEM_B, _ITEM_C = 90_000_001, 90_000_002, 90_000_003
_CTX_1, _CTX_2 = 30_001, 30_002


@pytest.fixture
def seeded_user():
    cluster = Cluster(HOSTS)
    session = cluster.connect(KEYSPACE)

    session.execute(
        "INSERT INTO candidates (user_id, rank, item_id, score) VALUES (%s, %s, %s, %s)",
        (_TEST_USER_ID, 1, _ITEM_A, 9.0),
    )
    session.execute(
        "INSERT INTO candidates (user_id, rank, item_id, score) VALUES (%s, %s, %s, %s)",
        (_TEST_USER_ID, 2, _ITEM_B, 8.0),
    )
    session.execute(
        "INSERT INTO candidates (user_id, rank, item_id, score) VALUES (%s, %s, %s, %s)",
        (_TEST_USER_ID, 3, _ITEM_C, 7.0),
    )
    for item_id, ctx in [(_ITEM_A, _CTX_1), (_ITEM_B, _CTX_2), (_ITEM_C, _CTX_1), (_ITEM_C, _CTX_2)]:
        session.execute(
            "INSERT INTO item_contexts (item_id, context_id) VALUES (%s, %s)", (item_id, ctx)
        )
    for ctx, item_id, rank, score in [
        (_CTX_1, _ITEM_A, 1, 9.0),
        (_CTX_1, _ITEM_C, 2, 7.0),
        (_CTX_2, _ITEM_B, 1, 8.0),
        (_CTX_2, _ITEM_C, 2, 7.0),
    ]:
        session.execute(
            "INSERT INTO candidates_by_context (context_id, user_id, rank, item_id, score) "
            "VALUES (%s, %s, %s, %s, %s)",
            (ctx, _TEST_USER_ID, rank, item_id, score),
        )
    session.execute(
        "INSERT INTO prematerialized (user_id, context_id, rank, item_id, score) "
        "VALUES (%s, %s, %s, %s, %s)",
        (_TEST_USER_ID, _CTX_1, 1, _ITEM_A, 9.0),
    )
    session.execute(
        "INSERT INTO prematerialized (user_id, context_id, rank, item_id, score) "
        "VALUES (%s, %s, %s, %s, %s)",
        (_TEST_USER_ID, _CTX_1, 2, _ITEM_C, 7.0),
    )

    yield _TEST_USER_ID

    session.execute("DELETE FROM candidates WHERE user_id = %s", (_TEST_USER_ID,))
    session.execute("DELETE FROM item_contexts WHERE item_id = %s", (_ITEM_A,))
    session.execute("DELETE FROM item_contexts WHERE item_id = %s", (_ITEM_B,))
    session.execute("DELETE FROM item_contexts WHERE item_id = %s", (_ITEM_C,))
    session.execute(
        "DELETE FROM candidates_by_context WHERE context_id = %s AND user_id = %s",
        (_CTX_1, _TEST_USER_ID),
    )
    session.execute(
        "DELETE FROM candidates_by_context WHERE context_id = %s AND user_id = %s",
        (_CTX_2, _TEST_USER_ID),
    )
    session.execute(
        "DELETE FROM prematerialized WHERE user_id = %s AND context_id = %s",
        (_TEST_USER_ID, _CTX_1),
    )
    cluster.shutdown()


async def test_get_candidates_returns_all_items(seeded_user):
    # Pertença item->contexto não viaja mais em Candidate (Fase 2.6,
    # catálogo em memória — core/contract.py:Candidate só tem
    # item_id/score); essa cobertura mora em strategies/tests/, não aqui.
    adapter = ScyllaAdapter(HOSTS)
    result = await adapter.get_candidates(seeded_user)
    assert {c.item_id for c in result} == {_ITEM_A, _ITEM_B, _ITEM_C}


async def test_get_candidates_filtered_single_context(seeded_user):
    adapter = ScyllaAdapter(HOSTS)
    result = await adapter.get_candidates_filtered(seeded_user, [_CTX_1])
    assert {c.item_id for c in result} == {_ITEM_A, _ITEM_C}


async def test_get_candidates_filtered_is_and_across_contexts(seeded_user):
    adapter = ScyllaAdapter(HOSTS)
    result = await adapter.get_candidates_filtered(seeded_user, [_CTX_1, _CTX_2])
    assert {c.item_id for c in result} == {_ITEM_C}


async def test_get_prematerialized_returns_stored_items(seeded_user):
    adapter = ScyllaAdapter(HOSTS)
    result = await adapter.get_prematerialized(seeded_user, str(_CTX_1))
    assert {c.item_id for c in result} == {_ITEM_A, _ITEM_C}


async def test_unknown_user_returns_empty_list_not_error():
    adapter = ScyllaAdapter(HOSTS)
    result = await adapter.get_candidates(999_999_999)
    assert result == []
