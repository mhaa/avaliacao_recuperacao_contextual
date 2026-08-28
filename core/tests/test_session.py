from __future__ import annotations

import inspect

from core.contract import Candidate
from core.session import apply_exclusion


def _candidates(ids):
    return [Candidate(item_id=item_id, score=1.0) for item_id in ids]


def test_exclusion_removes_only_excluded_items():
    candidates = _candidates([1, 2, 3, 4])
    result = apply_exclusion(candidates, [2, 4])
    assert [c.item_id for c in result] == [1, 3]


def test_empty_exclusion_is_noop():
    candidates = _candidates([1, 2, 3])
    result = apply_exclusion(candidates, [])
    assert [c.item_id for c in result] == [1, 2, 3]


def test_exclusion_ids_not_present_dont_affect_result():
    candidates = _candidates([1, 2, 3])
    result = apply_exclusion(candidates, [999])
    assert [c.item_id for c in result] == [1, 2, 3]


def test_signature_has_no_storage_argument():
    """Estruturalmente, a exclusão nunca pode virar parâmetro de uma consulta
    ao banco: a função nem aceita um adaptador de storage como argumento."""
    params = list(inspect.signature(apply_exclusion).parameters)
    assert params == ["candidates", "exclude_ids"]
