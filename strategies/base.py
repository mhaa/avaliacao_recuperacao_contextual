"""Interface comum às estratégias de recuperação (E-1..E-4).

`required_primitives` é comparado contra `storage.supported_primitives` em
`check_compatibility`, chamada na montagem da célula — nunca em tempo de
requisição (mesmo princípio de `storage/base.py`).

`prepare` é o gancho de montagem assíncrona: tudo que uma estratégia
precisa carregar do banco UMA vez, antes de servir a primeira requisição,
acontece ali. Hoje só E-1 e E-3 usam (o catálogo item->contexto de
`core/catalog.py`); E-2 e E-4 delegam o predicado ao banco e não precisam
de nada. Existe porque a montagem da célula (`core/registry.py`) é
síncrona e não pode fazer I/O — quem chama `prepare` é o dono do event
loop: `service/http_app.py` no lifespan, `harness/verify_cli.py` e os
testes de aceitação.
"""

from __future__ import annotations

from typing import Protocol

from core.contract import Request, Response
from storage.base import PrimitiveNotSupported, StorageAdapter


class Strategy(Protocol):
    name: str
    required_primitives: frozenset[str]

    async def prepare(self, storage: StorageAdapter) -> None: ...

    async def retrieve(self, storage: StorageAdapter, req: Request) -> Response: ...


def check_compatibility(strategy: Strategy, storage: StorageAdapter) -> None:
    missing = strategy.required_primitives - storage.supported_primitives
    if missing:
        raise PrimitiveNotSupported(sorted(missing)[0], storage.name)


async def build_cell_runtime(strategy: Strategy, storage: StorageAdapter) -> None:
    """Verificação de compatibilidade + carga de montagem, na ordem certa.
    Ponto único para os quatro donos de event loop que montam uma célula
    (service/http_app.py, service/main.py, harness/verify_cli.py e os testes
    de aceitação) — sem isso, esquecer o `prepare` num deles só apareceria
    como E-1 devolvendo resposta vazia em tempo de requisição.
    """
    check_compatibility(strategy, storage)
    await strategy.prepare(storage)
