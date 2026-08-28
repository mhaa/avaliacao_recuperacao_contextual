"""Regra de ordenação e desempate, compartilhada por todas as estratégias.

Mesma regra usada por `data_generation/generator/oracle.py` (via
candidates.parquet, já ordenado por `rank`) e `generator/rank.py` ao
atribuir o rank: score decrescente, com item_id crescente como desempate.
Vive aqui, e não dentro de cada estratégia, para que nenhuma célula (E-1..E-4)
precise reimplementar — e possivelmente diverja em — a mesma regra.
"""

from __future__ import annotations

from .contract import Candidate, ResponseItem


def order_candidates(candidates: list[Candidate]) -> list[Candidate]:
    return sorted(candidates, key=lambda c: (-c.score, c.item_id))


def assign_ranks(ordered_candidates: list[Candidate]) -> list[ResponseItem]:
    return [
        ResponseItem(item_id=c.item_id, score=c.score, rank=i + 1)
        for i, c in enumerate(ordered_candidates)
    ]


def rank_candidates(candidates: list[Candidate]) -> list[ResponseItem]:
    return assign_ranks(order_candidates(candidates))
