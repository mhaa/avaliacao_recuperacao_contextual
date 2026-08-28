"""Exige data_generation/data/oracle.parquet gerado:

    docker compose run --rm generator all --sample-users 10000 --seed 42
"""

from __future__ import annotations

import pytest

from harness.oracle import EXPECTED_CASE_COUNT, load_oracle_cases

pytestmark = pytest.mark.integration


def test_oracle_has_exactly_1000_cases():
    cases = load_oracle_cases()
    assert len(cases) == EXPECTED_CASE_COUNT


def test_oracle_case_schema_fields_present():
    case = load_oracle_cases()[0]
    assert isinstance(case.case_id, int)
    assert isinstance(case.user_id, int)
    assert isinstance(case.context_ids, list)
    assert isinstance(case.exclude_ids, list)
    assert isinstance(case.k, int)
    assert isinstance(case.expected_item_ids, list)
    assert isinstance(case.expected_scores, list)


def test_649_cases_have_fewer_than_k_expected_items():
    """Guarda de regressão: se este número mudar, o oráculo em disco não é
    mais o que este arnês foi calibrado para verificar — regenerar com os
    mesmos parâmetros (--sample-users 10000 --seed 42) ou atualizar este
    teste conscientemente, nunca silenciosamente."""
    cases = load_oracle_cases()
    short_cases = [c for c in cases if len(c.expected_item_ids) < c.k]
    assert len(short_cases) == 649
