"""E-1 — filtro na aplicação: lê todos os candidatos do usuário e avalia o
predicado categórico no processo do serviço, não no banco.

O predicado é avaliado contra o catálogo item->contexto residente em
memória (`core/catalog.py`), carregado uma vez em `prepare`. Isso é o que a
definição de E-1 pede — "avalia o predicado NO PROCESSO DO SERVIÇO" — e é
deliberado: antes, cada adaptador reconstruía a pertença ao contexto a cada
requisição, a um custo ditado pelo modelo de dados do adaptador (501
comandos no Valkey, 501 consultas CQL no ScyllaDB, `LEFT JOIN` +
`array_agg` no PostgreSQL, nada no OpenSearch), o que fazia esta linha da
matriz comparar adaptadores em vez de tecnologias. Ver CONTEXTO.md,
"Catálogo item->contexto residente na aplicação".

Do banco, portanto, E-1 exige só a leitura em massa dos candidatos
(`get_candidates`, devolvendo `(item_id, score)`) e o despejo único do
catálogo (`load_item_contexts`).
"""

from __future__ import annotations

from core.catalog import ItemCatalog, load_catalog
from core.contract import Request, Response, build_response
from core.ordering import rank_candidates
from core.session import apply_exclusion
from storage.base import GET_CANDIDATES, LOAD_ITEM_CONTEXTS, StorageAdapter


class E1AppFilter:
    name = "e1_app_filter"
    required_primitives = frozenset({GET_CANDIDATES, LOAD_ITEM_CONTEXTS})

    def __init__(self, catalog: ItemCatalog | None = None):
        self._catalog = catalog

    async def prepare(self, storage: StorageAdapter) -> None:
        if self._catalog is None:
            self._catalog = await load_catalog(storage)

    async def retrieve(self, storage: StorageAdapter, req: Request) -> Response:
        if self._catalog is None:
            raise RuntimeError("E1AppFilter.prepare() não foi chamada antes de retrieve()")
        candidates = await storage.get_candidates(req.user_id)
        candidates = self._catalog.filter(candidates, req.context)
        candidates = apply_exclusion(candidates, req.exclude)
        ranked = rank_candidates(candidates)
        return build_response(ranked, req.k)
