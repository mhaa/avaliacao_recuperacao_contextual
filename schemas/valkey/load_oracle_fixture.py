"""Carrega, em um Valkey vazio, os dados de harness/fixtures.py necessários
para verificar os 1000 casos do oráculo. Ver storage/valkey.py para o
esquema de chaves.

Uso:
    docker compose up -d valkey
    docker compose run --rm --entrypoint python tools schemas/valkey/load_oracle_fixture.py
"""

from __future__ import annotations

import os

import valkey

from harness import fixtures

URL = os.environ.get("TEST_VALKEY_URL", "redis://valkey:6379/0")


def main() -> None:
    client = valkey.Valkey.from_url(URL, decode_responses=True)
    client.flushdb()

    user_ids = fixtures.needed_user_ids()
    candidates = fixtures.load_candidates(user_ids)
    item_contexts = fixtures.load_item_contexts()
    prematerialized = fixtures.load_prematerialized(user_ids)
    inverted_lists = fixtures.load_inverted_lists()

    pipe = client.pipeline(transaction=False)

    for user_id, group in candidates.group_by("user_id"):
        (uid,) = user_id
        mapping = {str(row["item_id"]): row["score"] for row in group.iter_rows(named=True)}
        pipe.hset(f"candidates:{uid}", mapping=mapping)
        pipe.sadd(f"candidates_set:{uid}", *mapping.keys())

    for item_id, group in item_contexts.group_by("item_id"):
        (iid,) = item_id
        context_ids = group["context_id"].to_list()
        pipe.sadd(f"item_contexts:{iid}", *[str(c) for c in context_ids])

    for (user_id, context_id), group in prematerialized.group_by(["user_id", "context_id"]):
        mapping = {str(row["item_id"]): row["score"] for row in group.iter_rows(named=True)}
        if mapping:
            pipe.hset(f"prematerialized:{user_id}:{context_id}", mapping=mapping)

    for row in inverted_lists.iter_rows(named=True):
        item_ids = row["item_ids"]
        if item_ids:
            pipe.sadd(f"inverted:{row['context_id']}", *[str(i) for i in item_ids])

    pipe.execute()

    print(
        f"Carregado: {candidates.height} candidatos, {item_contexts.height} pertences "
        f"item-contexto, {prematerialized.height} linhas pré-materializadas, "
        f"{inverted_lists.height} listas invertidas, {len(user_ids)} usuários "
        "referenciados pelo oráculo"
    )


if __name__ == "__main__":
    main()
