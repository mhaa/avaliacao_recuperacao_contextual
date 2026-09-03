"""Catálogo item->contexto residente na memória do serviço.

Dado de catálogo: estático, O(itens) (~87.585 itens, ~200 mil pares
item-contexto), igual para todos os usuários, sem nenhuma informação de
ranking. Carregado UMA vez na montagem da célula
(`storage.load_item_contexts`, primitiva de carga em massa) e consultado em
memória a cada requisição — nunca relido do banco no caminho quente.

Por que existe (ver CONTEXTO.md, "Catálogo item->contexto residente na
aplicação"): sem ele, cada adaptador reconstruía `context_ids` por
requisição a um custo ditado pelo modelo de dados do adaptador e não pela
tecnologia sob teste — 1 round-trip com `LEFT JOIN` + `array_agg` no
PostgreSQL, de graça no OpenSearch (desnormalizado na carga), mas 501
comandos numa thread única no Valkey e 501 consultas CQL em ~8 ondas
sequenciais no ScyllaDB. A linha E-1 da matriz passava a medir qualidade de
adaptador em vez de tecnologia.

Usado por E-1 (todo o caminho) e por E-3 (só no caminho de contexto
composto, onde E-3 cai para leitura completa + filtro em aplicação).
"""

from __future__ import annotations

from core.contract import Candidate


class ItemCatalog:
    """Pertença item -> conjunto de contextos, imutável após a construção."""

    __slots__ = ("_contexts_by_item",)

    def __init__(self, contexts_by_item: dict[int, frozenset[int]]):
        self._contexts_by_item = contexts_by_item

    def __len__(self) -> int:
        return len(self._contexts_by_item)

    def contexts_of(self, item_id: int) -> frozenset[int]:
        return self._contexts_by_item.get(item_id, frozenset())

    def filter(self, candidates: list[Candidate], context: list[int]) -> list[Candidate]:
        """Aplica o predicado categórico com semântica AND — um item só passa
        se pertencer a TODOS os contextos pedidos (mesma regra do oráculo e de
        `generator/contexts.py`). Contexto vazio não filtra nada.
        """
        if not context:
            return candidates
        wanted = frozenset(context)
        by_item = self._contexts_by_item
        empty: frozenset[int] = frozenset()
        return [c for c in candidates if wanted <= by_item.get(c.item_id, empty)]
