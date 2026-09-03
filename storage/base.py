"""Primitivas que cada banco pode ou não oferecer — é aqui que a matriz de
viabilidade (docs/DESIGN.md) fica codificada.

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
# Primitiva de CARGA, não de requisição: chamada uma única vez na montagem
# da célula (`Strategy.prepare`), nunca no caminho quente. Ver
# core/catalog.py e docs/DESIGN.md, "Catálogo item->contexto residente na
# aplicação".
LOAD_ITEM_CONTEXTS = "load_item_contexts"

ALL_PRIMITIVES = frozenset(
    {
        GET_CANDIDATES,
        GET_CANDIDATES_FILTERED,
        GET_PREMATERIALIZED,
        INTERSECT,
        LOAD_ITEM_CONTEXTS,
    }
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

    async def load_item_contexts(self) -> dict[int, frozenset[int]]:
        """Despejo COMPLETO da pertença item->contexto, para o catálogo em
        memória do serviço. Chamada uma vez na montagem da célula — pode ser
        cara (varredura da estrutura inteira), nunca entra no caminho de
        requisição.
        """
        raise PrimitiveNotSupported(LOAD_ITEM_CONTEXTS, self.name)
