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
"""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from hypercorn.asyncio import serve as _hypercorn_serve
from hypercorn.config import Config as HypercornConfig

from core.contract import Request, Response
from storage.base import StorageAdapter
from strategies.base import Strategy


def create_app(strategy: Strategy, storage: StorageAdapter) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/recommendations", response_model=Response)
    async def recommendations(req: Request) -> Response:
        return await strategy.retrieve(storage, req)

    return app


def serve(
    strategy: Strategy,
    storage: StorageAdapter,
    host: str = "0.0.0.0",
    port: int = 8000,
) -> None:
    app = create_app(strategy, storage)
    config = HypercornConfig()
    config.bind = [f"{host}:{port}"]
    asyncio.run(_hypercorn_serve(app, config))
