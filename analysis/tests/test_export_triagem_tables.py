"""Testa analysis/export_triagem_tables.py contra uma árvore results/
sintética — mesma disciplina de dados sintéticos de
analysis/tests/test_report.py, sem depender de uma medição real."""

from __future__ import annotations

import json

from analysis.export_triagem_tables import (
    build_latencias_long_rows,
    build_resultados_rows,
    build_saturacao_sondagens_rows,
    estrategia_of,
    export,
)
from analysis.report import discover_rep_dirs, ensure_collected, load_cell_latencies


def _request_line(time: str, latency_ms: float) -> str:
    return json.dumps(
        {
            "request_id": "0-0",
            "scenario": "measurement",
            "timestamp": time,
            "latency_ms": latency_ms,
            "status": 200,
            "returned_count": 20,
        }
    )


def _write_fake_run(rep_dir, latencies: list[float]) -> None:
    rep_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        _request_line(f"2026-01-01T00:02:{i % 60:02d}.000Z", latency)
        for i, latency in enumerate(latencies)
    ]
    (rep_dir / "requests.ndjson").write_text("\n".join(lines) + "\n")


def _build_fake_results(tmp_path, phase="triagem"):
    cells = {
        "e1-postgres": [10.0, 20.0, 30.0, 40.0, 50.0],
        "e4-valkey": [1.0, 2.0, 3.0, 4.0, 5.0],
    }
    for cell_id, latencies in cells.items():
        rep_dir = tmp_path / cell_id / phase / "20260101T000000Z" / "rep0"
        _write_fake_run(rep_dir, latencies)
    return tmp_path, cells


def _write_fake_saturation(tmp_path, cell_id, phase="triagem"):
    saturation_dir = tmp_path / cell_id / phase / "20260101T000000Z"
    saturation_dir.mkdir(parents=True, exist_ok=True)
    (saturation_dir / "saturation.json").write_text(
        json.dumps(
            {
                "approx_throughput": 500.0,
                "censored": False,
                "lower_bound": None,
                "loadgen_bottleneck": False,
                "generator_cpu_unmeasured": False,
                "probes": [
                    {
                        "rate": 500,
                        "violated_slo": False,
                        "generator_cpu_percent": 5.0,
                        "p99_ms": 40.0,
                        "error_rate": 0.0,
                    },
                    {
                        "rate": 1000,
                        "violated_slo": True,
                        "generator_cpu_percent": 8.0,
                        "p99_ms": 900.0,
                        "error_rate": 0.0,
                    },
                ],
            }
        )
    )


def test_estrategia_of_extracts_the_strategy_prefix():
    assert estrategia_of("e4-postgres") == "E4"
    assert estrategia_of("e1-valkey") == "E1"


def test_build_resultados_rows_aggregates_across_repetitions(tmp_path):
    results_root, cells = _build_fake_results(tmp_path)
    _write_fake_saturation(tmp_path, "e1-postgres")
    rep_dirs = discover_rep_dirs(results_root, "triagem")
    ensure_collected(rep_dirs)
    groups = load_cell_latencies(rep_dirs)

    from analysis.report import load_cell_saturation

    saturation_by_cell = load_cell_saturation(rep_dirs)
    rows = build_resultados_rows(rep_dirs, groups, saturation_by_cell)
    by_id = {row["cell_id"]: row for row in rows}

    assert by_id["e1-postgres"]["banco"] == "postgres"
    assert by_id["e1-postgres"]["estrategia"] == "E1"
    assert by_id["e1-postgres"]["n_repeticoes"] == 1
    assert by_id["e1-postgres"]["n_requisicoes_total"] == 5
    assert by_id["e1-postgres"]["latencia_p50_ms"] == 30.0
    assert by_id["e1-postgres"]["vazao_saturacao_aprox_rps"] == 500.0
    # e4-valkey nunca teve saturation.json escrito — precisa ficar None, não
    # erro nem 0 (0 seria indistinguível de "célula ociosa medida").
    assert by_id["e4-valkey"]["vazao_saturacao_aprox_rps"] is None


def test_build_latencias_long_rows_has_one_row_per_percentile(tmp_path):
    results_root, cells = _build_fake_results(tmp_path)
    rep_dirs = discover_rep_dirs(results_root, "triagem")
    ensure_collected(rep_dirs)
    groups = load_cell_latencies(rep_dirs)

    rows = build_latencias_long_rows(groups)
    e1_rows = [r for r in rows if r["cell_id"] == "e1-postgres"]
    assert {r["percentil"] for r in e1_rows} == {"p50", "p95", "p99", "p999"}


def test_build_saturacao_sondagens_rows_expands_probes():
    saturation_by_cell = {
        "e1-postgres": {
            "probes": [
                {
                    "rate": 500,
                    "violated_slo": False,
                    "generator_cpu_percent": 5.0,
                    "p99_ms": 40.0,
                    "error_rate": 0.0,
                }
            ]
        }
    }
    rows = build_saturacao_sondagens_rows(saturation_by_cell)
    assert rows == [
        {
            "cell_id": "e1-postgres",
            "estrategia": "E1",
            "banco": "postgres",
            "vazao_sondada_rps": 500,
            "p99_ms": 40.0,
            "violou_slo": False,
            "cpu_gerador_pct": 5.0,
            "taxa_erro": 0.0,
        }
    ]


def test_export_writes_all_four_csv_files(tmp_path):
    results_root, cells = _build_fake_results(tmp_path)
    _write_fake_saturation(tmp_path, "e1-postgres")
    out_dir = tmp_path / "out"

    export(results_root, "triagem", out_dir)

    for name in (
        "triagem_resultados.csv",
        "triagem_resultados_ptbr.csv",
        "triagem_latencias_long.csv",
        "triagem_saturacao_sondagens.csv",
    ):
        assert (out_dir / name).exists()

    ptbr_content = (out_dir / "triagem_resultados_ptbr.csv").read_text(encoding="utf-8-sig")
    assert ";" in ptbr_content.splitlines()[0]
    # decimal vírgula: latência 30.0 vira "30,0" na variante ptbr.
    assert "30,0" in ptbr_content
