"""Testes das figuras da rampa de estresse (analysis/plots.py).

Arquivo próprio: analysis/tests/test_plots.py cobre as figuras da bateria
principal e não é editado. Como lá, a asserção é só "o PNG nasceu" — matplotlib
com backend Agg não é testado quanto a pixels, e o valor aqui é garantir que
nenhuma combinação de dados (degrau sem percentil, rampa vazia, série ausente)
derruba a geração."""

from __future__ import annotations

from analysis.plots import (
    plot_offered_vs_sustained,
    plot_percentiles_by_step,
    plot_resources_by_step,
)


def _step(rate, phase, *, throughput=None, p99=100.0, ok=True):
    return {
        "rate": rate,
        "phase": phase,
        "throughput_rps": throughput if throughput is not None else float(rate),
        "latency_ms_p50": 2.0,
        "latency_ms_p95": 3.0,
        "latency_ms_p99": p99,
        "latency_ms_p999": p99 * 2,
        "offered_load_ok": ok,
    }


def _ramp():
    return [
        _step(1000, "coarse_up"),
        _step(4000, "fine_up"),
        _step(6000, "fine_up", throughput=5200.0, p99=800.0, ok=False),
        _step(6000, "plateau", throughput=5000.0, p99=3000.0, ok=False),
        _step(4000, "fine_down", p99=2500.0),
    ]


def test_offered_vs_sustained_writes_a_png(tmp_path):
    out = tmp_path / "offered_vs_sustained.png"
    plot_offered_vs_sustained(_ramp(), out)
    assert out.exists()


def test_percentiles_by_step_writes_a_png(tmp_path):
    out = tmp_path / "percentiles.png"
    plot_percentiles_by_step(_ramp(), out)
    assert out.exists()


def test_resources_by_step_writes_a_png(tmp_path):
    out = tmp_path / "resources.png"
    plot_resources_by_step(
        cpu_by_component={
            "database (agregado)": [(1000, 5.0), (4000, 20.0)],
            "database (máx. por núcleo)": [(1000, 40.0), (4000, 99.0)],
            "service": [(1000, 10.0), (4000, 45.0)],
            "loadgen": [(1000, 8.0), (4000, 30.0)],
        },
        memory_by_component={"database": [(1000, 20000.0), (4000, 21000.0)]},
        out_path=out,
    )
    assert out.exists()


def test_empty_ramp_still_produces_a_figure(tmp_path):
    # Uma execução abortada cedo não pode derrubar a geração de relatório —
    # a figura vazia é o sinal de que não houve dado.
    for name, fn in (
        ("offered.png", plot_offered_vs_sustained),
        ("percentiles.png", plot_percentiles_by_step),
    ):
        out = tmp_path / name
        fn([], out)
        assert out.exists()


def test_steps_missing_a_percentile_do_not_break_the_figure(tmp_path):
    # build_summary devolve None em todos os percentis quando o degrau não
    # teve requisição nenhuma.
    steps = [
        {
            "rate": 1000,
            "phase": "fine_up",
            "throughput_rps": None,
            "latency_ms_p50": None,
            "latency_ms_p95": None,
            "latency_ms_p99": None,
            "offered_load_ok": None,
        }
    ]
    out = tmp_path / "percentiles.png"
    plot_percentiles_by_step(steps, out)
    assert out.exists()


def test_resources_figure_tolerates_a_missing_series(tmp_path):
    # Rede e memória são best-effort no coletor; uma série vazia é estado
    # normal, não erro.
    out = tmp_path / "resources.png"
    plot_resources_by_step(
        cpu_by_component={"database (agregado)": [], "service": [(1000, 10.0)]},
        memory_by_component={},
        out_path=out,
    )
    assert out.exists()
