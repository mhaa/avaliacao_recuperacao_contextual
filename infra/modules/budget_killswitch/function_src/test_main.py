# Testa só decide() — lógica pura, sem client GCP nenhum (nunca chama
# billing_v1 de verdade). Roda no container `tools` via pytest, junto do
# resto da suíte (pyproject.toml: testpaths inclui este diretório).

from main import KILL_THRESHOLD, decide


def test_ignores_notifications_below_threshold():
    assert decide(cost_amount=100.0, budget_amount=200.0, billing_enabled=True) == "ignore"


def test_ignores_exactly_at_100_percent():
    assert decide(cost_amount=200.0, budget_amount=200.0, billing_enabled=True) == "ignore"


def test_disables_when_threshold_crossed_and_billing_enabled():
    assert decide(cost_amount=240.0, budget_amount=200.0, billing_enabled=True) == "disable"


def test_noop_when_threshold_crossed_but_already_disabled():
    assert decide(cost_amount=240.0, budget_amount=200.0, billing_enabled=False) == "already_disabled"


def test_threshold_boundary_is_inclusive():
    boundary_cost = KILL_THRESHOLD * 200.0
    assert decide(cost_amount=boundary_cost, budget_amount=200.0, billing_enabled=True) == "disable"


def test_rejects_non_positive_budget():
    import pytest

    with pytest.raises(ValueError):
        decide(cost_amount=10.0, budget_amount=0.0, billing_enabled=True)
