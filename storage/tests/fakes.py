"""Adaptador fake em memória — usado pelos testes de estratégia (sem
banco). "Completo": implementa as 4 primitivas, para servir também as
estratégias das etapas seguintes (E-2..E-4) sem precisar de um fake novo
por estratégia. `self.calls` registra toda chamada, para testes de
regressão (ex.: E-3/E-4 não podem chamar as primitivas com exclusão).
"""

from __future__ import annotations

from core.contract import Candidate
from storage.base import (
    GET_CANDIDATES,
    GET_CANDIDATES_FILTERED,
    GET_PREMATERIALIZED,
    INTERSECT,
    StorageAdapter,
)


class FakeStorageAdapter(StorageAdapter):
    name = "fake"
    supported_primitives = frozenset(
        {GET_CANDIDATES, GET_CANDIDATES_FILTERED, GET_PREMATERIALIZED, INTERSECT}
    )

    def __init__(
        self,
        candidates_by_user: dict[int, list[Candidate]] | None = None,
        prematerialized: dict[tuple[int, str], list[Candidate]] | None = None,
        inverted_lists: dict[int, frozenset[int]] | None = None,
    ):
        self._candidates_by_user = candidates_by_user or {}
        self._prematerialized = prematerialized or {}
        self._inverted_lists = inverted_lists or {}
        self.calls: list[tuple[str, tuple]] = []

    async def get_candidates(self, user_id: int) -> list[Candidate]:
        self.calls.append((GET_CANDIDATES, (user_id,)))
        return list(self._candidates_by_user.get(user_id, []))

    async def get_candidates_filtered(self, user_id: int, context: list[int]) -> list[Candidate]:
        self.calls.append((GET_CANDIDATES_FILTERED, (user_id, tuple(context))))
        candidates = self._candidates_by_user.get(user_id, [])
        if not context:
            return list(candidates)
        wanted = frozenset(context)
        return [c for c in candidates if wanted <= c.context_ids]

    async def get_prematerialized(self, user_id: int, context_key: str) -> list[Candidate]:
        self.calls.append((GET_PREMATERIALIZED, (user_id, context_key)))
        return list(self._prematerialized.get((user_id, context_key), []))

    async def intersect(self, user_id: int, context: list[int], limit: int) -> list[Candidate]:
        self.calls.append((INTERSECT, (user_id, tuple(context))))
        candidates = self._candidates_by_user.get(user_id, [])
        wanted: frozenset[int] | None = None
        for context_id in context:
            items = self._inverted_lists.get(context_id, frozenset())
            wanted = items if wanted is None else (wanted & items)
        wanted = wanted or frozenset()
        return [c for c in candidates if c.item_id in wanted][:limit]
