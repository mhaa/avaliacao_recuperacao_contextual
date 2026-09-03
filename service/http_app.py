"""Serviço HTTP — FastAPI, servido via Hypercorn (não uvicorn: uvicorn não
implementa HTTP/2; um proxy reverso só para T-B introduziria um processo
extra cujo uso de CPU contaminaria a medição de recursos do serviço — ver
CONTEXTO.md, seção "Pilha", atualizada para citar Hypercorn no lugar de
uvicorn).

T-A (HTTP/1.1) e T-B (HTTP/2 cleartext) são o MESMO processo: rodando sem
TLS, o Hypercorn aceita tanto requisições HTTP/1.1 puras quanto o upgrade
`Upgrade: h2c` automaticamente — não existe uma flag "ligar HTTP/2" a
configurar aqui. A distinção T-A/T-B vive inteiramente do lado do cliente
de carga (qual protocolo o k6 escolhe usar em cada cenário), não do lado
do servidor.

Multi-processo: usa `hypercorn.run.run()` (não `hypercorn.asyncio.serve()`),
que spawna `config.workers` processos via `multiprocessing` ("spawn"),
todos escutando na mesma porta (`config.create_sockets()`). Medido ao
vivo: sem isso, o serviço nunca passava de ~1 core mesmo numa VM de 4
vCPUs (n2-standard-4) — confirmado travando as primeiras 4 células
medidas em fila sem limite (latências de 10-24s a RATE=1000, CPU do
serviço preso em ~28%). Como "spawn" não herda objetos vivos do processo
pai, cada worker precisa se montar sozinho a partir de CELL — por isso
`app` existe como variável de módulo (carregada via
`application_path="service.http_app:app"`, reimportada do zero em cada
worker) em vez de receber strategy/storage já prontos por argumento.
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI
from hypercorn.config import Config as HypercornConfig
from hypercorn.run import run as _hypercorn_run

from core.config import load_cell_config
from core.contract import Request, Response, ResponseItem, build_response
from core.registry import build_storage, build_strategy
from storage.base import StorageAdapter
from strategies.base import Strategy, check_compatibility

# asyncio.to_thread (usado por storage/valkey.py, storage/scylla.py e
# storage/opensearch.py para envolver clientes síncronos) usa por padrão
# um ThreadPoolExecutor de min(32, cpu_count+4) threads — 8 numa VM de 4
# vCPUs. Teto de concorrência bem mais apertado que a capacidade real
# desses bancos: confirmado ao vivo, fila sem limite (10-24s de latência a
# RATE=1000) mesmo com o banco ocioso. Valor alto o bastante, por worker,
# para nunca ser o gargalo.
_TO_THREAD_MAX_WORKERS = 64

# Valores sintéticos de /v1/baseline. Não são arbitrários: o piso só é
# subtraível da latência das células se o payload tiver o MESMO tamanho que
# uma resposta real, e o tamanho depende de quantos dígitos cada campo gera
# em JSON. Item ids do MovieLens vão até 87.585 (5 dígitos) e os scores do
# ALS serializam com várias casas — daí a base de 5 dígitos e o passo que
# mantém 6 casas decimais. Garantido por
# test_baseline_payload_matches_real_response_size.
_BASELINE_FIRST_ITEM_ID = 10_000
_BASELINE_SCORE_STEP = 0.000_997


@asynccontextmanager
async def _lifespan(app: FastAPI):
    asyncio.get_running_loop().set_default_executor(
        ThreadPoolExecutor(max_workers=_TO_THREAD_MAX_WORKERS)
    )
    # Carga de montagem (hoje: o catálogo item->contexto de E-1/E-3) — aqui,
    # e não em create_app, porque exige um event loop rodando e porque cada
    # worker Hypercorn se monta sozinho (multiprocessing "spawn"): cada um
    # carrega a própria cópia do catálogo, ~20-40 MB numa VM de 16 GB.
    await app.state.strategy.prepare(app.state.storage)
    yield


def create_app(strategy: Strategy, storage: StorageAdapter) -> FastAPI:
    app = FastAPI(lifespan=_lifespan)
    app.state.strategy = strategy
    app.state.storage = storage

    @app.post("/v1/recommendations", response_model=Response)
    async def recommendations(req: Request) -> Response:
        return await strategy.retrieve(storage, req)

    @app.post("/v1/baseline", response_model=Response)
    async def baseline(req: Request) -> Response:
        """Piso da bancada: mesmo caminho de requisição, sem banco nenhum.

        Mede o custo que NENHUMA célula consegue evitar — parse Pydantic da
        requisição, construção dos k ResponseItem, revalidação pelo
        `response_model`, serialização JSON e os dois hops de rede. Toda
        comparação entre células vira então "latência da célula menos este
        piso", e a pergunta "isso é o banco ou a minha aplicação?" passa a
        ter resposta numérica (benchmark.md, seção 6).

        Exercita exatamente o mesmo caminho de `/v1/recommendations` exceto
        a leitura e a filtragem: aceita o mesmo corpo (paga o mesmo parse de
        `context`/`exclude`) e devolve k itens de payload equivalente — há
        teste garantindo essa equivalência, que é o que torna a subtração
        legítima.

        Não toca `strategy` nem `storage`, de propósito. Existe em toda
        célula para que o piso seja medível no MESMO deploy que está sendo
        medido, e o k6 chega aqui só apontando TARGET_URL para esta rota
        (load/scenarios.js), sem cenário novo.
        """
        items = [
            ResponseItem(
                item_id=_BASELINE_FIRST_ITEM_ID + i,
                score=round(1.0 - i * _BASELINE_SCORE_STEP, 6),
                rank=i + 1,
            )
            for i in range(req.k)
        ]
        return build_response(items, req.k)

    return app


def _build_app_from_env() -> FastAPI:
    config = load_cell_config(os.environ["CELL"])
    strategy = build_strategy(config)
    storage = build_storage(config)
    check_compatibility(strategy, storage)
    return create_app(strategy, storage)


# Variável de módulo: cada worker gerado por `hypercorn.run.run()`
# reimporta este arquivo do zero (multiprocessing "spawn") e resolve
# application_path="service.http_app:app" aqui — precisa existir no nível
# do módulo, não só dentro de `serve()`. Fica None quando importado sem
# CELL definido (ex.: service/tests/test_http_app.py, que usa só
# create_app diretamente com um storage fake).
app = _build_app_from_env() if "CELL" in os.environ else None


def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    config = HypercornConfig()
    config.bind = [f"{host}:{port}"]
    config.workers = os.cpu_count() or 1
    config.application_path = "service.http_app:app"
    _hypercorn_run(config)
