"""E-4 — interseção de conjuntos: intersecta os candidatos do usuário com a
lista invertida (global, por contexto) do(s) contexto(s) pedidos. Ao
contrário de E-3, as listas invertidas não são truncadas (ver
data_generation/README.md: `inverted_lists.parquet` guarda TODOS os itens
de cada contexto) — por isso `intersect` já lida corretamente com múltiplos
`context_ids` (interseção AND) sem o problema de truncamento de E-3.

`limit` é passado generoso (N=500, o teto de candidatos por usuário do
desenho experimental — ver CONTEXTO.md) para nunca cortar antes da
exclusão de sessão ser aplicada.
"""

from __future__ import annotations

from core.contract import Request, Response, build_response
from core.ordering import rank_candidates
from core.session import apply_exclusion
from storage.base import INTERSECT, StorageAdapter

_N_CANDIDATES = 500


class E4Intersection:
    name = "e4_intersection"
    required_primitives = frozenset({INTERSECT})

    async def prepare(self, storage: StorageAdapter) -> None:
        """Nada a carregar: o predicado é resolvido dentro do banco, então
        esta estratégia não precisa do catálogo item->contexto em memória."""
        return None

    async def retrieve(self, storage: StorageAdapter, req: Request) -> Response:
        candidates = await storage.intersect(req.user_id, req.context, _N_CANDIDATES)
        candidates = apply_exclusion(candidates, req.exclude)
        ranked = rank_candidates(candidates)
        return build_response(ranked, req.k)
