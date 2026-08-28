"""Testes de conformidade do adaptador Valkey — exigem um Valkey real:

    docker compose up -d valkey
    docker compose run --rm tools -m integration storage/tests/test_valkey_adapter.py -v

Usa user_id E item_id fora da faixa real de dados (mesmo cuidado de
storage/tests/test_postgres_adapter.py). `inverted:{context_id}` é uma
chave GLOBAL compartilhada com dado real carregado por
schemas/valkey/load_oracle_fixture.py — o teardown usa SREM (remove só os
membros de teste), nunca DEL na chave inteira.
"""

from __future__ import annotations

import os

import pytest
import valkey

from storage.valkey import ValkeyAdapter

pytestmark = pytest.mark.integration

URL = os.environ.get("TEST_VALKEY_URL", "redis://valkey:6379/0")

_TEST_USER_ID = 999_001
_TEST_ITEM_IDS = ["90000001", "90000002", "90000003"]
_TEST_CONTEXT_IDS = ["900001", "900002"]


def _client() -> valkey.Valkey:
    return valkey.Valkey.from_url(URL, decode_responses=True)


@pytest.fixture
def seeded_user():
    client = _client()
    i1, i2, i3 = _TEST_ITEM_IDS
    c1, c2 = _TEST_CONTEXT_IDS

    client.hset(f"candidates:{_TEST_USER_ID}", mapping={i1: "9.0", i2: "8.0", i3: "7.0"})
    client.sadd(f"candidates_set:{_TEST_USER_ID}", i1, i2, i3)
    client.sadd(f"item_contexts:{i1}", c1)
    client.sadd(f"item_contexts:{i2}", c2)
    client.sadd(f"item_contexts:{i3}", c1, c2)
    client.sadd(f"inverted:{c1}", i1, i3)
    client.sadd(f"inverted:{c2}", i2, i3)
    client.hset(f"prematerialized:{_TEST_USER_ID}:{c1}", mapping={i1: "9.0", i3: "7.0"})

    yield _TEST_USER_ID

    client.delete(
        f"candidates:{_TEST_USER_ID}",
        f"candidates_set:{_TEST_USER_ID}",
        f"item_contexts:{i1}",
        f"item_contexts:{i2}",
        f"item_contexts:{i3}",
        f"prematerialized:{_TEST_USER_ID}:{c1}",
    )
    client.srem(f"inverted:{c1}", i1, i3)
    client.srem(f"inverted:{c2}", i2, i3)


async def test_get_candidates_returns_all_with_context_ids(seeded_user):
    adapter = ValkeyAdapter(URL)
    result = await adapter.get_candidates(seeded_user)
    by_id = {c.item_id: c for c in result}
    assert set(by_id) == {90000001, 90000002, 90000003}
    assert by_id[90000001].context_ids == frozenset({900001})
    assert by_id[90000003].context_ids == frozenset({900001, 900002})


async def test_get_candidates_filtered_returns_only_matching_context(seeded_user):
    adapter = ValkeyAdapter(URL)
    result = await adapter.get_candidates_filtered(seeded_user, [900001])
    assert {c.item_id for c in result} == {90000001, 90000003}


async def test_get_candidates_filtered_is_and_across_contexts(seeded_user):
    adapter = ValkeyAdapter(URL)
    result = await adapter.get_candidates_filtered(seeded_user, [900001, 900002])
    assert {c.item_id for c in result} == {90000003}


async def test_get_prematerialized_returns_stored_items(seeded_user):
    adapter = ValkeyAdapter(URL)
    result = await adapter.get_prematerialized(seeded_user, "900001")
    assert {c.item_id for c in result} == {90000001, 90000003}


async def test_intersect_single_context(seeded_user):
    adapter = ValkeyAdapter(URL)
    result = await adapter.intersect(seeded_user, [900001], limit=500)
    assert {c.item_id for c in result} == {90000001, 90000003}


async def test_intersect_composed_context_is_and(seeded_user):
    adapter = ValkeyAdapter(URL)
    result = await adapter.intersect(seeded_user, [900001, 900002], limit=500)
    assert {c.item_id for c in result} == {90000003}


async def test_unknown_user_returns_empty_list_not_error():
    adapter = ValkeyAdapter(URL)
    result = await adapter.get_candidates(999_999_999)
    assert result == []
