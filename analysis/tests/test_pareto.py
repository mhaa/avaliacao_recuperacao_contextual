"""Testes de analysis/pareto.py — dominância 2D (latência × custo por milhão
de requisições), com dados sintéticos de propriedade conhecida (mesma
disciplina de analysis/tests/test_stats.py)."""

from __future__ import annotations

import pytest

from analysis.pareto import (
    SECONDS_PER_MONTH,
    cells_without_cost,
    censorship_warning,
    cheapest_cells,
    cost_per_million_requests,
    cost_per_million_requests_bounds,
    dominates,
    pareto_frontier,
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


def test_cost_per_million_requests_normalizes_by_the_unit_s_own_saturation():
    # 1000 req/s de capacidade * 2.592.000 s/mês = 2,592 bilhões de req/mês.
    # Custo de 100/mês * 1e6 / 2,592e9 requisições.
    cell = _cell("a", latency=10, unit_cost=100, saturation=1000)
    expected = 100 * 1_000_000 / (1000 * SECONDS_PER_MONTH)
    assert cost_per_million_requests(cell) == pytest.approx(expected)


def test_cost_per_million_requests_is_higher_for_lower_saturation():
    # Mesmo custo por unidade, vazão menor -> menos requisições por mês pra
    # diluir o custo -> custo por milhão de requisições MAIOR.
    slow = _cell("slow", latency=10, unit_cost=100, saturation=500)
    fast = _cell("fast", latency=10, unit_cost=100, saturation=2000)
    assert cost_per_million_requests(slow) > cost_per_million_requests(fast)


def test_censored_cell_has_no_point_estimate_but_has_a_cost_ceiling():
    # S >= 50.000 (piso, não ponto): não dá pra saber o custo por requisição
    # exato (poderia ser muito menor que o teto), só um TETO via o piso.
    cell = _cell("c", latency=10, unit_cost=100, censored=True, lower_bound=50_000)
    assert cost_per_million_requests(cell) is None

    bounds = cost_per_million_requests_bounds(cell)
    assert bounds is not None
    cost_lo, cost_hi = bounds
    assert cost_lo == 0.0
    assert cost_hi == pytest.approx(100 * 1_000_000 / (50_000 * SECONDS_PER_MONTH))


def test_censored_cell_without_lower_bound_has_no_cost_at_all():
    cell = _cell("c", latency=10, unit_cost=100, censored=True, lower_bound=None)
    assert cost_per_million_requests(cell) is None
    assert cost_per_million_requests_bounds(cell) is None


def test_cell_without_saturation_has_undefined_cost_and_never_enters_the_frontier():
    priced = _cell("priced", latency=10, unit_cost=100, saturation=1000)
    # latência MELHOR que a da outra: mesmo assim não pode entrar na fronteira,
    # porque não há como posicioná-la no eixo de custo.
    unpriced = _cell("unpriced", latency=1, unit_cost=100, saturation=None)

    assert cost_per_million_requests(unpriced) is None
    assert {c["cell_id"] for c in pareto_frontier([priced, unpriced])} == {"priced"}

    without_cost = cells_without_cost([priced, unpriced])
    assert [c["cell_id"] for c in without_cost] == ["unpriced"]
    assert "vazão de saturação sem dado" in without_cost[0]["reason"]


def test_dominance_is_two_dimensional():
    fast_cheap = _cell("fast_cheap", latency=10, unit_cost=100, saturation=2000)
    slow_expensive = _cell("slow_expensive", latency=20, unit_cost=100, saturation=500)
    assert dominates(fast_cheap, slow_expensive) is True
    assert dominates(slow_expensive, fast_cheap) is False

    # Mais barata porém mais lenta: nenhuma domina a outra.
    cheap_slow = _cell("cheap_slow", latency=30, unit_cost=100, saturation=2000)
    fast_pricier = _cell("fast_pricier", latency=5, unit_cost=100, saturation=500)
    assert dominates(cheap_slow, fast_pricier) is False
    assert dominates(fast_pricier, cheap_slow) is False


def test_cost_tolerance_ties_cells_whose_saturation_differs_within_20_percent():
    a = _cell("a", latency=10, unit_cost=100, saturation=1000)
    b = _cell("b", latency=10, unit_cost=100, saturation=1150)  # +15%, dentro de 20%
    # As bandas de custo (±20% sobre S, invertida) se sobrepõem -> empate.
    assert dominates(a, b) is False
    assert dominates(b, a) is False


def test_cost_beyond_tolerance_dominates():
    a = _cell("a", latency=10, unit_cost=100, saturation=1000)
    b = _cell("b", latency=10, unit_cost=100, saturation=2000)  # 2x, muito além de 20%
    # b satura mais -> custo por requisição menor -> b domina a.
    assert dominates(b, a) is True
    assert dominates(a, b) is False


def test_memory_capacity_can_force_more_units_than_one():
    # É esta a distinção memória x disco dentro da fórmula: memória não tem
    # preço próprio (já está em p_i), ela limita quantas unidades cabem — e
    # o custo por milhão de requisições escala com esse piso.
    big = _cell(
        "big",
        latency=10,
        unit_cost=100,
        saturation=1000,
        memory_bytes=30 * GIB,
        memory_per_unit_bytes=24 * GIB,
    )
    small = _cell(
        "small",
        latency=10,
        unit_cost=100,
        saturation=1000,
        memory_bytes=3 * GIB,
        memory_per_unit_bytes=24 * GIB,
    )
    # big precisa de 2 unidades (⌈30/24⌉=2); small de 1 (termo inerte, como
    # nos dados reais de hoje) — mesmo custo por unidade e mesma vazão, big
    # sai com o dobro do custo por milhão de requisições.
    assert cost_per_million_requests(big) == pytest.approx(2 * cost_per_million_requests(small))


def test_memory_without_declared_capacity_is_an_error_not_a_silent_one_unit():
    broken = _cell("broken", latency=10, unit_cost=100, saturation=1000, memory_bytes=30 * GIB)
    broken["memory_per_unit_bytes"] = None
    with pytest.raises(ValueError, match="memory_per_unit_bytes"):
        cost_per_million_requests(broken)


def test_cheapest_reports_every_tied_cell_instead_of_breaking_the_tie():
    """Com memória fora do preço por GiB, as células Valkey ficam com custo
    por unidade idêntico. Desempatar por ordem alfabética afirmava que uma
    delas era a mais barata — afirmação falsa."""
    a = _cell("a-valkey", latency=10, unit_cost=425.37, saturation=1000)
    b = _cell("b-valkey", latency=20, unit_cost=425.37, saturation=1000)
    c = _cell("c-postgres", latency=5, unit_cost=428.15, saturation=1000)

    assert cheapest_cells([a, b, c]) == ["a-valkey", "b-valkey"]


def test_cheapest_cells_never_includes_a_censored_cell():
    # Censurada não tem ponto — mesmo com um teto de custo baixo, não entra
    # na comparação pontual de "mais barata".
    censored = _cell("c", latency=10, unit_cost=1.0, censored=True, lower_bound=1_000_000)
    plain = _cell("p", latency=10, unit_cost=100.0, saturation=1000)
    assert cheapest_cells([censored, plain]) == ["p"]


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
