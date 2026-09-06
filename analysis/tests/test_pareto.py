"""Testes de analysis/pareto.py — dominância 2D (latência × custo(D)), com
dados sintéticos de propriedade conhecida (mesma disciplina de
analysis/tests/test_stats.py)."""

from __future__ import annotations

import pytest

from analysis.pareto import (
    breakpoints,
    cells_without_cost,
    censorship_warning,
    cost_at,
    cost_curve,
    crossovers,
    demand_domain_max,
    dominates,
    frontier_segments,
    pareto_frontier,
    units_at,
    units_bounds_at,
)

GIB = 1024**3


def _cell(
    cell_id,
    latency,
    unit_cost,
    saturation=None,
    censored=False,
    lower_bound=None,
    memory_bytes=0,
    memory_per_unit_bytes=24 * GIB,
):
    return {
        "cell_id": cell_id,
        "latency_p99_ms": latency,
        "unit_cost_usd_month": unit_cost,
        "saturation_throughput_approx": saturation,
        "saturation_censored": censored,
        "saturation_lower_bound": lower_bound,
        "memory_bytes": memory_bytes,
        "memory_per_unit_bytes": memory_per_unit_bytes,
    }


def test_units_is_the_ceiling_of_demand_over_saturation():
    cell = _cell("a", latency=10, unit_cost=100, saturation=1000)
    assert units_at(cell, 1) == 1
    # D = S exatamente ainda é 1 unidade: o degrau é fechado à direita. É o
    # ponto que a aritmética em Fraction existe para não errar.
    assert units_at(cell, 1000) == 1
    assert units_at(cell, 1001) == 2
    assert units_at(cell, 3000) == 3


def test_cost_scales_with_units_because_each_unit_holds_a_full_replica():
    # Sem sharding: 2 unidades = 2 réplicas integrais, logo o custo por unidade
    # (computação + armazenamento) é multiplicado, não só a computação.
    cell = _cell("a", latency=10, unit_cost=427.64, saturation=1000)
    assert cost_at(cell, 1000) == pytest.approx(427.64)
    assert cost_at(cell, 2000) == pytest.approx(2 * 427.64)


def test_censored_cell_needs_exactly_one_unit_below_the_ceiling():
    cell = _cell("c", latency=10, unit_cost=100, censored=True, lower_bound=50_000)
    # S ≥ 50.000 e D ≤ 50.000 ⟹ ⌈D/S⌉ = 1 EXATAMENTE, sem imputar o teto.
    assert units_at(cell, 10_000) == 1
    assert cost_at(cell, 10_000) == 100
    # Acima do limite medido a célula é genuinamente indeterminada — nunca se
    # usa o teto como se fosse o S medido. Na prática `demand_domain_max`
    # impede que se chegue aqui.
    assert units_at(cell, 60_000) is None
    assert units_bounds_at(cell, 60_000) is None


def test_cell_without_saturation_has_undefined_cost_and_never_enters_the_frontier():
    priced = _cell("priced", latency=10, unit_cost=100, saturation=1000)
    # latência MELHOR que a da outra: mesmo assim não pode entrar na fronteira,
    # porque não há como posicioná-la no eixo de custo.
    unpriced = _cell("unpriced", latency=1, unit_cost=100, saturation=None)

    assert cost_at(unpriced, 500) is None
    assert {c["cell_id"] for c in pareto_frontier([priced, unpriced], 500)} == {"priced"}

    without_cost = cells_without_cost([priced, unpriced])
    assert [c["cell_id"] for c in without_cost] == ["unpriced"]
    assert "vazão de saturação sem dado" in without_cost[0]["reason"]


def test_dominance_is_two_dimensional_at_a_given_demand():
    fast_cheap = _cell("fast_cheap", latency=10, unit_cost=100, saturation=2000)
    slow_expensive = _cell("slow_expensive", latency=20, unit_cost=100, saturation=500)
    assert dominates(fast_cheap, slow_expensive, 2000) is True
    assert dominates(slow_expensive, fast_cheap, 2000) is False

    # Mais barata porém mais lenta: nenhuma domina a outra.
    cheap_slow = _cell("cheap_slow", latency=30, unit_cost=100, saturation=2000)
    fast_pricier = _cell("fast_pricier", latency=5, unit_cost=100, saturation=500)
    assert dominates(cheap_slow, fast_pricier, 2000) is False
    assert dominates(fast_pricier, cheap_slow, 2000) is False


def test_cost_tolerance_ties_cells_whose_saturation_differs_within_20_percent():
    a = _cell("a", latency=10, unit_cost=100, saturation=1000)
    b = _cell("b", latency=10, unit_cost=100, saturation=1150)  # +15%, dentro de 20%
    # Em D alto os intervalos [n_lo, n_hi] se sobrepõem -> empate.
    assert dominates(a, b, 10_000) is False
    assert dominates(b, a, 10_000) is False


def test_cost_beyond_tolerance_dominates():
    a = _cell("a", latency=10, unit_cost=100, saturation=1000)
    b = _cell("b", latency=10, unit_cost=100, saturation=2000)  # 2x, muito além de 20%
    assert dominates(b, a, 10_000) is True
    assert dominates(a, b, 10_000) is False


def test_tolerance_does_not_blur_cost_when_both_cells_need_one_unit():
    # Propriedade que mantém a parcela de armazenamento viva em demanda baixa:
    # com n = 1 nos dois extremos, o intervalo degenera num ponto e uma
    # diferença de US$ 1 discrimina.
    a = _cell("a", latency=10, unit_cost=100.0, saturation=1000)
    b = _cell("b", latency=10, unit_cost=101.0, saturation=1150)
    assert dominates(a, b, 100) is True


def test_breakpoints_are_the_multiples_of_saturation_and_of_its_tolerance_band():
    cell = _cell("a", latency=10, unit_cost=100, saturation=1000)
    # múltiplos de 1000 (S), 1200 (S·1,2) e 800 (S·0,8), abaixo de 2500
    assert [float(b) for b in breakpoints([cell], 2500)] == [800, 1000, 1200, 1600, 2000, 2400]


def test_frontier_segments_partition_the_domain_and_are_maximal():
    a = _cell("a", latency=10, unit_cost=100, saturation=1000)
    b = _cell("b", latency=20, unit_cost=110, saturation=1500)
    segments = frontier_segments([a, b], 3000)

    assert segments[0]["demand_from_rps_exclusive"] == 0.0
    assert segments[-1]["demand_to_rps_inclusive"] == 3000.0
    for previous, current in zip(segments, segments[1:]):
        # contíguos, sem lacuna
        assert previous["demand_to_rps_inclusive"] == current["demand_from_rps_exclusive"]
        # maximais: dois segmentos vizinhos nunca repetem a mesma decisão
        assert (previous["pareto_frontier"], previous["cheapest_cell_id"]) != (
            current["pareto_frontier"],
            current["cheapest_cell_id"],
        )


def test_crossovers_are_reported_where_the_cheapest_cell_changes():
    # Latências iguais para isolar o efeito de custo. A é mais barata por
    # unidade; B satura mais tarde. Argmin: A em (0,1000], B em (1000,1500],
    # A de novo em (1500,2000].
    a = _cell("a", latency=10, unit_cost=100, saturation=1000)
    b = _cell("b", latency=10, unit_cost=110, saturation=1500)
    changes = crossovers(frontier_segments([a, b], 2000), [a, b])

    demands = [c["demand_rps"] for c in changes["cost"]]
    assert 1000.0 in demands
    assert 1500.0 in demands


def test_crossover_reports_which_cell_added_a_unit():
    a = _cell("a", latency=10, unit_cost=100, saturation=1000)
    b = _cell("b", latency=10, unit_cost=110, saturation=1500)
    changes = crossovers(frontier_segments([a, b], 2000), [a, b])

    at_1000 = next(c for c in changes["cost"] if c["demand_rps"] == 1000.0)
    assert at_1000["units_incremented"] == ["a"]


def test_memory_capacity_can_force_more_units_than_throughput_alone():
    # É esta a distinção memória x disco dentro da fórmula: memória não tem
    # preço próprio (já está em p_i), ela limita quantas unidades cabem.
    big = _cell(
        "big",
        latency=10,
        unit_cost=100,
        saturation=10_000,
        memory_bytes=30 * GIB,
        memory_per_unit_bytes=24 * GIB,
    )
    assert units_at(big, 100) == 2  # capacidade manda, não a vazão

    small = _cell(
        "small",
        latency=10,
        unit_cost=100,
        saturation=10_000,
        memory_bytes=3 * GIB,
        memory_per_unit_bytes=24 * GIB,
    )
    assert units_at(small, 100) == 1  # termo inerte, como nos dados reais de hoje


def test_memory_without_declared_capacity_is_an_error_not_a_silent_one_unit():
    broken = _cell("broken", latency=10, unit_cost=100, saturation=1000, memory_bytes=30 * GIB)
    broken["memory_per_unit_bytes"] = None
    with pytest.raises(ValueError, match="memory_per_unit_bytes"):
        units_at(broken, 100)


def test_cost_curve_steps_follow_the_units():
    cell = _cell("a", latency=10, unit_cost=100, saturation=1000)
    steps = cost_curve(cell, 2500)
    assert steps[0] == {
        "demand_from_rps": 0.0,
        "demand_to_rps": 1000.0,
        "units": 1,
        "cost_usd_month": 100,
    }
    assert [s["units"] for s in steps] == [1, 2, 3]
    assert steps[-1]["demand_to_rps"] == 2500.0


def test_demand_domain_is_clamped_to_the_lowest_censored_bound():
    censored = _cell("c", 10, 100, censored=True, lower_bound=50_000)
    plain = _cell("p", 10, 100, saturation=1000)

    limit, warning = demand_domain_max([censored, plain], 80_000)
    assert float(limit) == 50_000.0
    assert warning is not None

    limit, warning = demand_domain_max([censored, plain], 10_000)
    assert float(limit) == 10_000.0
    assert warning is None


def test_censorship_warning_fires_when_more_than_half_are_censored():
    cells = [
        _cell("a", 10, 100, censored=True, lower_bound=50_000),
        _cell("b", 10, 100, censored=True, lower_bound=50_000),
        _cell("c", 10, 100, saturation=10_000),
    ]
    warning = censorship_warning(cells)
    assert warning is not None
    assert "2/3" in warning


def test_censorship_warning_is_none_when_few_are_censored():
    cells = [
        _cell("a", 10, 100, censored=True, lower_bound=50_000),
        _cell("b", 10, 100, saturation=10_000),
        _cell("c", 10, 100, saturation=10_000),
    ]
    assert censorship_warning(cells) is None
