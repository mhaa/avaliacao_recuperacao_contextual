from __future__ import annotations

from core.contract import ResponseItem, build_response


def _ranked(n):
    return [ResponseItem(item_id=i, score=1.0 - i * 0.01, rank=i + 1) for i in range(n)]


def test_fewer_items_than_k_returns_only_available():
    ranked = _ranked(5)
    response = build_response(ranked, k=20)
    assert len(response.items) == 5
    assert response.returned_count == 5


def test_never_backfills_from_another_source():
    ranked = _ranked(3)
    response = build_response(ranked, k=20)
    assert [item.item_id for item in response.items] == [0, 1, 2]


def test_zero_items_does_not_raise():
    response = build_response([], k=20)
    assert response.items == []
    assert response.returned_count == 0


def test_more_items_than_k_are_truncated():
    ranked = _ranked(30)
    response = build_response(ranked, k=20)
    assert len(response.items) == 20
    assert response.returned_count == 20
