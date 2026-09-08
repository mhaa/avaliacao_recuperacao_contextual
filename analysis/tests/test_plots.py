"""Testes de fumaça: cada função produz um PNG não vazio a partir de dados
sintéticos. Conteúdo de pixel não é verificado (não é o que garante que o
gráfico está certo) — o que importa aqui é que a função roda sem erro contra
o formato de dados esperado e escreve o arquivo no caminho pedido."""

from __future__ import annotations

from analysis.plots import (
    plot_cache_hit_rate,
    plot_latency_vs_throughput,
    plot_pareto_frontier,
    plot_percentile_comparison,
)


def test_plot_pareto_frontier_writes_a_png(tmp_path):
    cells = [
        {"cell_id": "e1-postgres", "latency_p99_ms": 45.0},
        {"cell_id": "e3-valkey", "latency_p99_ms": 8.0},
    ]
    out = tmp_path / "pareto.png"
    plot_pareto_frontier(
        cells,
        out,
        frontier_cell_ids={"e3-valkey"},
        cost_by_cell={"e1-postgres": 0.0459, "e3-valkey": 0.0117},
    )
    assert out.stat().st_size > 0


def test_plot_percentile_comparison_writes_a_png(tmp_path):
    percentiles_by_cell = {
        "e1-postgres": {"p50": 10.0, "p95": 20.0, "p99": 45.0, "p999": 60.0},
        "e3-valkey": {"p50": 2.0, "p95": 5.0, "p99": 8.0, "p999": 12.0},
    }
    out = tmp_path / "percentiles.png"
    plot_percentile_comparison(percentiles_by_cell, out)
    assert out.stat().st_size > 0


def test_plot_latency_vs_throughput_writes_a_png(tmp_path):
    points_by_cell = {"e1-postgres": [(100.0, 10.0), (1000.0, 45.0), (5000.0, 220.0)]}
    out = tmp_path / "ramp.png"
    plot_latency_vs_throughput(points_by_cell, out)
    assert out.stat().st_size > 0


def test_plot_cache_hit_rate_writes_a_png(tmp_path):
    out = tmp_path / "cache.png"
    plot_cache_hit_rate({"none": 0.0, "candidates": 0.82, "response": 0.01}, out)
    assert out.stat().st_size > 0
