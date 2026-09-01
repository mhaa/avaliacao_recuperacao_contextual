"""Carrega, em um Valkey vazio, a base completa (todos os usuários, não só
o subconjunto do oráculo — ver schemas/valkey/load_oracle_fixture.py para
esse) — usado pela bateria de medição real da Fase 5
(infra/scripts/run_measurement_battery.py), nunca pelo smoke test. Ver
storage/valkey.py para o esquema de chaves.

Uso:
    docker compose run --rm --entrypoint python tools schemas/valkey/load_full_dataset.py
"""

from __future__ import annotations

import os

import valkey

from harness import fixtures

URL = os.environ.get("TEST_VALKEY_URL", "redis://valkey:6379/0")
# Flush a cada N comandos enfileirados no pipeline, não só no final — sem
# isso, o cliente acumula TODOS os comandos da base completa (~100,5M
# candidatos + ~4M linhas pré-materializadas em escala real) em memória
# antes de mandar qualquer coisa pro servidor, e o processo morre por OOM
# (confirmado ao vivo: "Killed" na VM loadgen). schemas/postgres/
# load_full_dataset.py nunca teve esse problema porque `COPY ... FROM
# STDIN` já transmite linha a linha (streaming de verdade); pipeline do
# redis/valkey não — precisa de execute() periódico pra ter o mesmo efeito.
BATCH_SIZE = 5000


def main() -> None:
    fixtures.ensure_full_dataset_downloaded()

    client = valkey.Valkey.from_url(URL, decode_responses=True)
    client.flushdb()

    candidates = fixtures.load_candidates()
    item_contexts = fixtures.load_item_contexts()
    prematerialized = fixtures.load_prematerialized()
    inverted_lists = fixtures.load_inverted_lists()

    pipe = client.pipeline(transaction=False)
    pending = 0

    def queue() -> None:
        nonlocal pending
        pending += 1
        if pending >= BATCH_SIZE:
            pipe.execute()
            pending = 0

    # Print periódico por usuário processado — maior dos 4 laços (até
    # ~200 mil usuários em escala real) e o único onde silêncio total
    # durante minutos seria indistinguível de travado. Mesmo raciocínio de
    # schemas/scylla/load_full_dataset.py (label em
    # _execute_concurrent_batched): sem isso, só o print no fim de main()
    # dá qualquer sinal de vida.
    users_done = 0
    for user_id, group in candidates.group_by("user_id"):
        (uid,) = user_id
        mapping = {str(row["item_id"]): row["score"] for row in group.iter_rows(named=True)}
        pipe.hset(f"candidates:{uid}", mapping=mapping)
        queue()
        pipe.sadd(f"candidates_set:{uid}", *mapping.keys())
        queue()
        users_done += 1
        if users_done % 20_000 == 0:
            print(f"candidates: {users_done} usuários gravados", flush=True)
    print(f"candidates: {users_done} usuários gravados (final)", flush=True)

    for item_id, group in item_contexts.group_by("item_id"):
        (iid,) = item_id
        context_ids = group["context_id"].to_list()
        pipe.sadd(f"item_contexts:{iid}", *[str(c) for c in context_ids])
        queue()
    print(f"item_contexts: {item_contexts.height} linhas gravadas", flush=True)

    for (user_id, context_id), group in prematerialized.group_by(["user_id", "context_id"]):
        mapping = {str(row["item_id"]): row["score"] for row in group.iter_rows(named=True)}
        if mapping:
            pipe.hset(f"prematerialized:{user_id}:{context_id}", mapping=mapping)
            queue()
    print(f"prematerialized: {prematerialized.height} linhas gravadas", flush=True)

    for row in inverted_lists.iter_rows(named=True):
        item_ids = row["item_ids"]
        if item_ids:
            pipe.sadd(f"inverted:{row['context_id']}", *[str(i) for i in item_ids])
            queue()
    print(f"inverted_lists: {inverted_lists.height} linhas gravadas", flush=True)

    pipe.execute()

    print(
        f"Carregado: {candidates.height} candidatos, {item_contexts.height} pertences "
        f"item-contexto, {prematerialized.height} linhas pré-materializadas, "
        f"{inverted_lists.height} listas invertidas (base completa)"
    )


if __name__ == "__main__":
    main()
