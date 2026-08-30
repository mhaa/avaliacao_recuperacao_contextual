"""Testes de analysis/pareto.py — dominância de Pareto em 3 dimensões, com
dados sintéticos de propriedade conhecida (mesma disciplina de
analysis/tests/test_stats.py)."""

from __future__ import annotations

from analysis.pareto import censorship_warning, dominates, pareto_frontier, throughput_dominates


def _cell(cell_id, latency, cost, throughput=None, censored=False):
    return {
        "cell_id": cell_id,
        "latency_p99_ms": latency,
        "cost_usd_hour": cost,
        "saturation_throughput_approx": throughput,
        "saturation_censored": censored,
    }


def test_pareto_frontier_keeps_only_the_clear_winner():
    winner = _cell("winner", latency=10, cost=0.5, throughput=20_000)
    dominated_a = _cell("a", latency=20, cost=0.5, throughput=20_000)  # pior latência só
    dominated_b = _cell("b", latency=10, cost=0.6, throughput=15_000)  # pior custo e vazão
    frontier_ids = {c["cell_id"] for c in pareto_frontier([winner, dominated_a, dominated_b])}
    assert frontier_ids == {"winner"}


def test_throughput_within_tolerance_is_treated_as_tied():
    a = _cell("a", latency=10, cost=0.5, throughput=10_000)
    b = _cell("b", latency=10, cost=0.5, throughput=10_500)  # 5% de diferença, dentro de 20%
    assert throughput_dominates(a, b) is False
    assert throughput_dominates(b, a) is False
    assert dominates(a, b) is False
    assert dominates(b, a) is False


def test_throughput_beyond_tolerance_dominates():
    a = _cell("a", latency=10, cost=0.5, throughput=10_000)
    b = _cell("b", latency=10, cost=0.5, throughput=13_000)  # 30% acima, além de 20%
    assert throughput_dominates(b, a) is True
    assert dominates(b, a) is True


def test_censored_cell_beats_a_non_censored_cell_with_higher_nominal_throughput():
    censored = _cell("censored", latency=10, cost=0.5, throughput=None, censored=True)
    # não-censurada com vazão nominal MAIOR que o teto — mesmo assim perde,
    # porque a regra é "censurada > qualquer não-censurada", nunca por valor.
    high_nominal = _cell("high_nominal", latency=10, cost=0.5, throughput=60_000)
    assert throughput_dominates(censored, high_nominal) is True
    assert dominates(censored, high_nominal) is True
    assert dominates(high_nominal, censored) is False


def test_two_censored_cells_are_tied_on_throughput():
    a = _cell("a", latency=10, cost=0.5, throughput=None, censored=True)
    b = _cell("b", latency=10, cost=0.5, throughput=None, censored=True)
    assert throughput_dominates(a, b) is False
    assert throughput_dominates(b, a) is False
    assert dominates(a, b) is False
    assert dominates(b, a) is False
    # nenhuma domina a outra -> ambas ficam na fronteira
    assert {c["cell_id"] for c in pareto_frontier([a, b])} == {"a", "b"}


def test_censorship_warning_fires_when_more_than_half_are_censored():
    cells = [
        _cell("a", 10, 0.5, censored=True),
        _cell("b", 10, 0.5, censored=True),
        _cell("c", 10, 0.5, throughput=10_000, censored=False),
    ]
    warning = censorship_warning(cells)
    assert warning is not None
    assert "2/3" in warning


def test_censorship_warning_is_none_when_few_are_censored():
    cells = [
        _cell("a", 10, 0.5, censored=True),
        _cell("b", 10, 0.5, throughput=10_000, censored=False),
        _cell("c", 10, 0.5, throughput=10_000, censored=False),
    ]
    assert censorship_warning(cells) is None
