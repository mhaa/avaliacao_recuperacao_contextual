"""Serviço gRPC — T-C (HTTP/2 + Protobuf). Espelha exatamente o mesmo
contrato de core/contract.py via service/proto/recommendation.proto.

Construído e testável agora (fazia parte do pedido de "service/"), mas
ainda NÃO entra em load/ nesta etapa: a Fase 2 (comparação de transporte,
incluindo T-C) só faz sentido depois que a Fase 1 escolher a célula
vencedora via medição real na nuvem — o que ainda não aconteceu.
"""

from __future__ import annotations

import asyncio

import grpc

from core.contract import Request as CoreRequest
from storage.base import StorageAdapter
from strategies.base import Strategy

from .proto import recommendation_pb2, recommendation_pb2_grpc


class RecommendationServicer(recommendation_pb2_grpc.RecommendationServicer):
    def __init__(self, strategy: Strategy, storage: StorageAdapter):
        self._strategy = strategy
        self._storage = storage

    async def Retrieve(self, request, grpc_context):
        core_req = CoreRequest(
            user_id=request.user_id,
            context=list(request.context),
            exclude=list(request.exclude),
            k=request.k,
        )
        response = await self._strategy.retrieve(self._storage, core_req)
        return recommendation_pb2.Response(
            items=[
                recommendation_pb2.ResponseItem(item_id=i.item_id, score=i.score, rank=i.rank)
                for i in response.items
            ],
            returned_count=response.returned_count,
        )


async def _serve_async(strategy: Strategy, storage: StorageAdapter, host: str, port: int) -> None:
    # Carga de montagem (catálogo item->contexto de E-1/E-3) antes de o
    # servidor aceitar a primeira requisição. Aqui, e não em service/main.py,
    # porque é este o dono do event loop — o HTTP faz o equivalente no
    # lifespan de cada worker Hypercorn (service/http_app.py).
    await strategy.prepare(storage)
    server = grpc.aio.server()
    recommendation_pb2_grpc.add_RecommendationServicer_to_server(
        RecommendationServicer(strategy, storage), server
    )
    server.add_insecure_port(f"{host}:{port}")
    await server.start()
    await server.wait_for_termination()


def serve(
    strategy: Strategy,
    storage: StorageAdapter,
    host: str = "0.0.0.0",
    port: int = 50051,
) -> None:
    asyncio.run(_serve_async(strategy, storage, host, port))
