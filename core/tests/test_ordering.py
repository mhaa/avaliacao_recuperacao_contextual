from __future__ import annotations

import random

from core.contract import Candidate
from core.ordering import order_candidates, rank_candidates


def _candidates(pairs):
    return [Candidate(item_id=item_id, score=score) for item_id, score in pairs]


def test_orders_by_score_desc_then_item_id_asc_on_tie():
    candidates = _candidates([(3, 1.0), (1, 1.0), (2, 2.0)])
    ordered = order_candidates(candidates)
    assert [c.item_id for c in ordered] == [2, 1, 3]


def test_ranks_assigned_contiguously_from_one():
    candidates = _candidates([(10, 0.5), (20, 0.9), (30, 0.7)])
    ranked = rank_candidates(candidates)
    assert [r.rank for r in ranked] == [1, 2, 3]
    assert [r.item_id for r in ranked] == [20, 30, 10]


def test_idempotent_under_input_reordering():
    pairs = [(i, float(i % 7)) for i in range(50)]
    candidates = _candidates(pairs)
    shuffled = list(candidates)
    random.Random(42).shuffle(shuffled)

    result_original = [c.item_id for c in order_candidates(candidates)]
    result_shuffled = [c.item_id for c in order_candidates(shuffled)]
    assert result_original == result_shuffled


def test_no_candidates_returns_empty_list():
    assert order_candidates([]) == []
    assert rank_candidates([]) == []
