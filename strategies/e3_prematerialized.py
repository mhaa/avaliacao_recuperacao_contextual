"""E-3 — pré-materialização por contexto: leitura direta por chave
(usuário, contexto).

Decisão de implementação (descoberta ao cobrir os casos "composed" do
oráculo, não antecipada no pseudocódigo original de docs/ARCHITECTURE.md):
`prematerialized.parquet` é uma linha por (usuário, CONTEXTO ÚNICO),
truncada em top-M=40 por esse contexto isolado — ver
data_generation/README.md. Intersectar dois top-40 (um por contexto) NÃO
garante o mesmo resultado que o oráculo, que filtra sobre os 500
candidatos completos: um item pode estar fora do top-40 de um contexto
isoladamente e ainda assim estar no topo da interseção AND de dois
contextos. Por isso, para requisições com mais de um `context_id`, E-3
cai para leitura completa + filtro em aplicação (mesmo caminho de E-1) —
resultado correto, ao custo de não usar a pré-materialização nesse caso.
Isso é esperado virar um resultado citável do TCC (E-3 é rápido para
contexto único, mas não tem vantagem em contexto composto), não um bug.

Por isso `required_primitives` inclui `get_candidates` além de
`get_prematerialized` — e, desde a adoção do catálogo em memória, também
`load_item_contexts`: esse caminho de fallback avalia o predicado na
aplicação exatamente como E-1, contra `core/catalog.py` (ver docs/DESIGN.md,
"Catálogo item->contexto residente na aplicação"). Requisição de contexto
único e requisição sem contexto não tocam o catálogo.
"""

from __future__ import annotations

from core.catalog import ItemCatalog, load_catalog
from core.contract import Request, Response, build_response
from core.ordering import rank_candidates
from core.session import apply_exclusion
from storage.base import (
    GET_CANDIDATES,
    GET_PREMATERIALIZED,
    LOAD_ITEM_CONTEXTS,
    StorageAdapter,
)


class E3Prematerialized:
    name = "e3_prematerialized"
    required_primitives = frozenset(
        {GET_PREMATERIALIZED, GET_CANDIDATES, LOAD_ITEM_CONTEXTS}
    )

    def __init__(self, catalog: ItemCatalog | None = None):
        self._catalog = catalog

    async def prepare(self, storage: StorageAdapter) -> None:
        if self._catalog is None:
            self._catalog = await load_catalog(storage)

    async def retrieve(self, storage: StorageAdapter, req: Request) -> Response:
        if len(req.context) == 1:
            candidates = await storage.get_prematerialized(req.user_id, str(req.context[0]))
        elif not req.context:
            candidates = await storage.get_candidates(req.user_id)
        else:
            if self._catalog is None:
                raise RuntimeError(
                    "E3Prematerialized.prepare() não foi chamada antes de retrieve()"
                )
            candidates = await storage.get_candidates(req.user_id)
            candidates = self._catalog.filter(candidates, req.context)
        candidates = apply_exclusion(candidates, req.exclude)
        ranked = rank_candidates(candidates)
        return build_response(ranked, req.k)
