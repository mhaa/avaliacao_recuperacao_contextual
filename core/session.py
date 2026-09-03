"""Exclusão de itens da sessão.

Aplicada na camada de aplicação, em TODAS as estratégias — inclusive E-3
(pré-materializado) e E-4 (interseção), cujos artefatos de origem já vêm
sem a exclusão aplicada. Nenhuma estratégia deve empurrar isso para o banco
(ver `docs/ARCHITECTURE.md`, "## Não fazer"): por isso esta função nem aceita um
adaptador de storage como argumento — estruturalmente não há como fazer a
exclusão virar um parâmetro de consulta.
"""

from __future__ import annotations

from .contract import Candidate


def apply_exclusion(candidates: list[Candidate], exclude_ids: list[int]) -> list[Candidate]:
    if not exclude_ids:
        return list(candidates)
    excluded = set(exclude_ids)
    return [c for c in candidates if c.item_id not in excluded]
