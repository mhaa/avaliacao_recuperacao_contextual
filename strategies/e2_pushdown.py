"""E-2 — predicado delegado ao banco: o banco já devolve só os itens
elegíveis (`get_candidates_filtered`). Exclusão de sessão continua sendo
aplicada na aplicação, nunca no banco (ver core/session.py).
"""

from __future__ import annotations

from core.contract import Request, Response, build_response
from core.ordering import rank_candidates
from core.session import apply_exclusion
from storage.base import GET_CANDIDATES_FILTERED, StorageAdapter


class E2Pushdown:
    name = "e2_pushdown"
    required_primitives = frozenset({GET_CANDIDATES_FILTERED})

    async def retrieve(self, storage: StorageAdapter, req: Request) -> Response:
        candidates = await storage.get_candidates_filtered(req.user_id, req.context)
        candidates = apply_exclusion(candidates, req.exclude)
        ranked = rank_candidates(candidates)
        return build_response(ranked, req.k)
