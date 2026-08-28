from __future__ import annotations

from core.contract import ResponseItem
from harness.oracle import OracleCase
from harness.verify import verify_case


def _case(expected_item_ids, expected_scores):
    return OracleCase(
        case_id=1,
        user_id=1,
        context_ids=[],
        exclude_ids=[],
        k=20,
        expected_item_ids=expected_item_ids,
        expected_scores=expected_scores,
    )


def _items(pairs):
    return [ResponseItem(item_id=i, score=s, rank=idx + 1) for idx, (i, s) in enumerate(pairs)]


def test_exact_match_passes():
    case = _case([10, 20], [5.0, 4.0])
    result = verify_case(case, _items([(10, 5.0), (20, 4.0)]))
    assert result.passed


def test_same_items_different_order_fails():
    """Ordem importa — mesmo conjunto de itens em ordem diferente é falha."""
    case = _case([10, 20], [5.0, 4.0])
    result = verify_case(case, _items([(20, 4.0), (10, 5.0)]))
    assert not result.passed


def test_short_result_matching_oracle_passes():
    """649 dos 1000 casos do oráculo têm menos de k itens — curto não é
    suspeito quando bate com o que o oráculo já encodou como correto."""
    case = _case([10], [5.0])
    result = verify_case(case, _items([(10, 5.0)]))
    assert result.passed


def test_extra_item_fails():
    case = _case([10], [5.0])
    result = verify_case(case, _items([(10, 5.0), (20, 4.0)]))
    assert not result.passed


def test_missing_item_fails():
    case = _case([10, 20], [5.0, 4.0])
    result = verify_case(case, _items([(10, 5.0)]))
    assert not result.passed


def test_score_within_tolerance_passes():
    case = _case([10], [5.0])
    result = verify_case(case, _items([(10, 5.0 + 1e-6)]))
    assert result.passed


def test_score_beyond_tolerance_fails():
    case = _case([10], [5.0])
    result = verify_case(case, _items([(10, 5.0 + 1e-3)]))
    assert not result.passed
