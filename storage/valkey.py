"""Adaptador Valkey (BD-2).

Decisão de implementação: a imagem hoje pinada em docker-compose.yml
(`valkey/valkey:8-alpine`) não traz um módulo de busca (RediSearch ou
equivalente), que é a técnica que CONTEXTO.md original imaginava para E-2.
Em vez de depender de uma imagem de módulo (não há uma tag oficial estável
para isso no momento da implementação), E-2 e E-4 são implementados com
primitivas nativas do Valkey:

- `candidates:{user_id}` — HASH item_id -> score.
- `item_contexts:{item_id}` — SET de context_ids (pertença do item, dado de
  catálogo). Lido em MASSA uma única vez, na montagem da célula
  (`load_item_contexts`, via SCAN + SMEMBERS em lotes), para o catálogo em
  memória de `core/catalog.py`; e usado por `get_candidates_filtered` via um
  script Lua que avalia o predicado server-side (SISMEMBER por item, dentro
  do Valkey — nunca busca tudo para o cliente filtrar).

  `get_candidates` (E-1) NÃO toca mais essas chaves: emitia um pipeline de
  500 SMEMBERS por requisição, 501 comandos numa thread única, o que fazia
  a célula E-1/Valkey medir a normalização escolhida aqui em vez do custo
  real de uma leitura em massa (ver CONTEXTO.md, "Catálogo item->contexto
  residente na aplicação").
- `catalog:item_contexts` — HASH item_id -> context_ids separados por
  vírgula, gravado pelos loaders. É a fonte do despejo de montagem
  (`load_item_contexts`), redundante de propósito com as chaves por item:
  enumerá-las exigiria SCAN sobre o keyspace INTEIRO (~4,4 milhões de
  chaves na base cheia para achar ~80 mil), com custo proporcional ao total
  e não ao catálogo — 4 workers subindo juntos saturariam a thread única do
  Valkey por dezenas de segundos. Com o hash, é um HGETALL só.
- `candidates_set:{user_id}` — SET de item_id (mesma informação de
  `candidates:{user_id}`, só que como SET, para permitir SINTERSTORE).
- `inverted:{context_id}` — SET de item_id (lista invertida global, não
  truncada — ver data_generation/README.md). `intersect` usa SINTERSTORE
  entre `candidates_set` e as listas invertidas pedidas.
- `prematerialized:{user_id}:{context_id}` — HASH item_id -> score (top-40).

O predicado continua avaliado DENTRO do banco nos dois casos (a distinção
real que CONTEXTO.md quer entre E-1 e as demais), ainda que a técnica não
seja literalmente um módulo de busca — registrar essa divergência no texto
do TCC.

Cliente síncrono (`valkey-py`) rodado via `asyncio.to_thread`, pela mesma
razão de não haver necessidade de pool/async nesta etapa (foco em
corretude, não performance — ver storage/postgres.py).
"""

from __future__ import annotations

import asyncio

import valkey

from core.contract import Candidate

from .base import (
    GET_CANDIDATES,
    GET_CANDIDATES_FILTERED,
    GET_PREMATERIALIZED,
    INTERSECT,
    LOAD_ITEM_CONTEXTS,
    StorageAdapter,
)

# Ver docstring: o catálogo vem de um hash único, não de um SCAN do keyspace.
_CATALOG_KEY = "catalog:item_contexts"

_FILTER_SCRIPT = """
local result = {}
local all = redis.call('HGETALL', KEYS[1])
for i = 1, #all, 2 do
    local item_id = all[i]
    local score = all[i + 1]
    local matches = true
    for j = 1, #ARGV do
        if redis.call('SISMEMBER', 'item_contexts:' .. item_id, ARGV[j]) == 0 then
            matches = false
            break
        end
    end
    if matches then
        table.insert(result, item_id)
        table.insert(result, score)
    end
end
return result
"""


class ValkeyAdapter(StorageAdapter):
    name = "valkey"
    supported_primitives = frozenset(
        {
            GET_CANDIDATES,
            GET_CANDIDATES_FILTERED,
            GET_PREMATERIALIZED,
            INTERSECT,
            LOAD_ITEM_CONTEXTS,
        }
    )

    def __init__(self, url: str):
        self._client = valkey.Valkey.from_url(url, decode_responses=True)
        self._filter_script = self._client.register_script(_FILTER_SCRIPT)

    async def get_candidates(self, user_id: int) -> list[Candidate]:
        return await asyncio.to_thread(self._get_candidates_sync, user_id)

    def _get_candidates_sync(self, user_id: int) -> list[Candidate]:
        raw = self._client.hgetall(f"candidates:{user_id}")
        return [
            Candidate(item_id=int(item_id), score=float(score))
            for item_id, score in raw.items()
        ]

    async def load_item_contexts(self) -> dict[int, frozenset[int]]:
        return await asyncio.to_thread(self._load_item_contexts_sync)

    def _load_item_contexts_sync(self) -> dict[int, frozenset[int]]:
        # EXISTS antes do HGETALL: um hash ausente e um catálogo legitimamente
        # vazio devolvem a mesma coisa ({}), e um catálogo vazio faria E-1
        # responder lista vazia para qualquer predicado — resposta errada, em
        # silêncio. É exatamente o modo de falha de subir contra um dado
        # carregado por um loader antigo. Falhar alto, na montagem.
        if not self._client.exists(_CATALOG_KEY):
            raise RuntimeError(
                f"chave '{_CATALOG_KEY}' não existe no Valkey — o dado foi carregado "
                "por uma versão anterior de schemas/valkey/load_*.py. Recarregue."
            )
        raw = self._client.hgetall(_CATALOG_KEY)
        return {
            int(item_id): frozenset(int(c) for c in contexts.split(",") if c)
            for item_id, contexts in raw.items()
        }

    async def get_candidates_filtered(self, user_id: int, context: list[int]) -> list[Candidate]:
        if not context:
            return await self.get_candidates(user_id)
        return await asyncio.to_thread(self._get_candidates_filtered_sync, user_id, context)

    def _get_candidates_filtered_sync(self, user_id: int, context: list[int]) -> list[Candidate]:
        flat = self._filter_script(
            keys=[f"candidates:{user_id}"], args=[str(c) for c in context]
        )
        return [
            Candidate(item_id=int(flat[i]), score=float(flat[i + 1]))
            for i in range(0, len(flat), 2)
        ]

    async def get_prematerialized(self, user_id: int, context_key: str) -> list[Candidate]:
        return await asyncio.to_thread(self._get_prematerialized_sync, user_id, context_key)

    def _get_prematerialized_sync(self, user_id: int, context_key: str) -> list[Candidate]:
        raw = self._client.hgetall(f"prematerialized:{user_id}:{context_key}")
        return [
            Candidate(item_id=int(item_id), score=float(score)) for item_id, score in raw.items()
        ]

    async def intersect(self, user_id: int, context: list[int], limit: int) -> list[Candidate]:
        if not context:
            return []
        return await asyncio.to_thread(self._intersect_sync, user_id, context, limit)

    def _intersect_sync(self, user_id: int, context: list[int], limit: int) -> list[Candidate]:
        # SINTER (não SINTERSTORE + SMEMBERS + DELETE): a interseção continua
        # sendo feita DENTRO do banco — que é o que define E-4 — mas em um
        # round-trip só e sem chave temporária. A versão anterior escrevia
        # `tmp:intersect:{user_id}:{contextos}`, uma chave COMPARTILHADA por
        # requisições concorrentes do mesmo (usuário, contexto): duas em voo
        # ao mesmo tempo podiam ter uma deletando o que a outra ainda ia ler.
        # Além da corrida, era escrita no caminho de leitura.
        keys = [f"candidates_set:{user_id}"] + [f"inverted:{c}" for c in context]
        item_ids = list(self._client.sinter(keys))
        if not item_ids:
            return []
        scores = self._client.hmget(f"candidates:{user_id}", item_ids)
        candidates = [
            Candidate(item_id=int(item_id), score=float(score))
            for item_id, score in zip(item_ids, scores)
            if score is not None
        ]
        candidates.sort(key=lambda c: -c.score)
        return candidates[:limit]
