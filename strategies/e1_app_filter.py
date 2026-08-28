"""E-1 — filtro na aplicação: lê todos os candidatos do usuário e avalia o
predicado categórico no processo do serviço, não no banco. Só exige
`get_candidates` do adaptador de storage.
"""

from __future__ import annotations

from core.contract import Request, Response, build_response
from core.ordering import rank_candidates
from core.session import apply_exclusion
from storage.base import GET_CANDIDATES, StorageAdapter


class E1AppFilter:
    name = "e1_app_filter"
    required_primitives = frozenset({GET_CANDIDATES})

    async def retrieve(self, storage: StorageAdapter, req: Request) -> Response:
        candidates = await storage.get_candidates(req.user_id)
        if req.context:
            wanted = frozenset(req.context)
            candidates = [c for c in candidates if wanted <= c.context_ids]
        candidates = apply_exclusion(candidates, req.exclude)
        ranked = rank_candidates(candidates)
        return build_response(ranked, req.k)
