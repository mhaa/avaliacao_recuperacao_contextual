"""Interface comum às estratégias de recuperação (E-1..E-4).

`required_primitives` é comparado contra `storage.supported_primitives` em
`check_compatibility`, chamada na montagem da célula — nunca em tempo de
requisição (mesmo princípio de `storage/base.py`).
"""

from __future__ import annotations

from typing import Protocol

from core.contract import Request, Response
from storage.base import PrimitiveNotSupported, StorageAdapter


class Strategy(Protocol):
    name: str
    required_primitives: frozenset[str]

    async def retrieve(self, storage: StorageAdapter, req: Request) -> Response: ...


def check_compatibility(strategy: Strategy, storage: StorageAdapter) -> None:
    missing = strategy.required_primitives - storage.supported_primitives
    if missing:
        raise PrimitiveNotSupported(sorted(missing)[0], storage.name)
