"""Primitivas que cada banco pode ou não oferecer — é aqui que a matriz de
viabilidade (CONTEXTO.md) fica codificada.

Cada adaptador real declara `supported_primitives`; a montagem da célula
(`strategies.base.check_compatibility`) compara isso contra o que a
estratégia exige e levanta `PrimitiveNotSupported` ali, na montagem —
nunca em tempo de requisição. Por padrão (nesta classe base), nenhuma
primitiva é suportada e cada método levanta a exceção — cada adaptador
sobrescreve só o que de fato implementa.
"""

from __future__ import annotations

from core.contract import Candidate

GET_CANDIDATES = "get_candidates"
GET_CANDIDATES_FILTERED = "get_candidates_filtered"
GET_PREMATERIALIZED = "get_prematerialized"
INTERSECT = "intersect"

ALL_PRIMITIVES = frozenset(
    {GET_CANDIDATES, GET_CANDIDATES_FILTERED, GET_PREMATERIALIZED, INTERSECT}
)


class PrimitiveNotSupported(Exception):
    def __init__(self, primitive: str, adapter_name: str):
        super().__init__(f"'{adapter_name}' não suporta a primitiva '{primitive}'")
        self.primitive = primitive
        self.adapter_name = adapter_name


class StorageAdapter:
    name: str = "unknown"
    supported_primitives: frozenset[str] = frozenset()

    async def get_candidates(self, user_id: int) -> list[Candidate]:
        raise PrimitiveNotSupported(GET_CANDIDATES, self.name)

    async def get_candidates_filtered(self, user_id: int, context: list[int]) -> list[Candidate]:
        raise PrimitiveNotSupported(GET_CANDIDATES_FILTERED, self.name)

    async def get_prematerialized(self, user_id: int, context_key: str) -> list[Candidate]:
        raise PrimitiveNotSupported(GET_PREMATERIALIZED, self.name)

    async def intersect(self, user_id: int, context: list[int], limit: int) -> list[Candidate]:
        raise PrimitiveNotSupported(INTERSECT, self.name)
