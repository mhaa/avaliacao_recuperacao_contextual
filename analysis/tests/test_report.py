"""Testa analysis/report.py contra uma árvore results/ sintética (3 células
fictícias, uma com latência deslocada de propósito) — mesma disciplina de
dados sintéticos com propriedade conhecida de analysis/tests/test_stats.py,
e o mesmo formato de NDJSON de analysis/tests/test_collect.py, sem depender
de k6 nem de uma medição real."""

from __future__ import annotations

import json

import numpy as np

from analysis.report import (
    build_report,
    discover_rep_dirs,
    ensure_collected,
    load_cell_latencies,
    load_cell_saturation,
)


def _request_line(scenario: str, time: str, latency_ms: float, status: int, request_id: str) -> str:
    return json.dumps(
        {
            "request_id": request_id,
            "scenario": scenario,
            "timestamp": time,
            "latency_ms": latency_ms,
            "status": status,
            "returned_count": 20,
        }
    )


def _write_fake_run(rep_dir, latencies: list[float]) -> None:
    rep_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, latency in enumerate(latencies):
        request_id = f"{i}-0"
        time = f"2026-01-01T00:02:{i % 60:02d}.000Z"
        lines.append(_request_line("measurement", time, latency, 200, request_id))
    (rep_dir / "requests.ndjson").write_text("\n".join(lines) + "\n")


def _build_fake_results(tmp_path, phase="triagem"):
    rng = np.random.default_rng(42)
    cells = {
        "e1-postgres": rng.normal(10, 1, 100).tolist(),
        "e2-postgres": rng.normal(10, 1, 100).tolist(),
        "e1-valkey": rng.normal(30, 1, 100).tolist(),  # deslocada de propósito
    }
    for cell_id, latencies in cells.items():
        rep_dir = tmp_path / cell_id / phase / "20260101T000000Z" / "rep0"
        _write_fake_run(rep_dir, latencies)
    return tmp_path, cells


def test_discover_rep_dirs_finds_every_cell_for_the_phase(tmp_path):
    results_root, cells = _build_fake_results(tmp_path)
    rep_dirs = discover_rep_dirs(results_root, "triagem")
    assert len(rep_dirs) == len(cells)
    assert not discover_rep_dirs(results_root, "confirmacao")


def test_ensure_collected_is_idempotent(tmp_path):
    results_root, _ = _build_fake_results(tmp_path)
    rep_dirs = discover_rep_dirs(results_root, "triagem")
    ensure_collected(rep_dirs)
    summary_mtimes = {p: (p / "summary.json").stat().st_mtime for p in rep_dirs}

    ensure_collected(rep_dirs)  # segunda chamada não deve recalcular

    for p in rep_dirs:
        assert (p / "summary.json").stat().st_mtime == summary_mtimes[p]


def test_load_cell_latencies_groups_by_cell(tmp_path):
    results_root, cells = _build_fake_results(tmp_path)
    rep_dirs = discover_rep_dirs(results_root, "triagem")
    ensure_collected(rep_dirs)

    groups = load_cell_latencies(rep_dirs)

    assert set(groups) == set(cells)
    assert len(groups["e1-postgres"]) == 100


def test_build_report_rejects_h0_and_dunn_points_at_the_shifted_cell(tmp_path):
    results_root, cells = _build_fake_results(tmp_path)
    rep_dirs = discover_rep_dirs(results_root, "triagem")
    ensure_collected(rep_dirs)
    groups = load_cell_latencies(rep_dirs)

    report = build_report(groups)

    assert report["kruskal_wallis"]["reject_h0"] is True
    assert report["dunn_posthoc"]["e1-postgres|e1-valkey"] < 0.05
    assert report["dunn_posthoc"]["e2-postgres|e1-valkey"] < 0.05
    assert report["dunn_posthoc"]["e1-postgres|e2-postgres"] > 0.05
    cell_ids = {c["cell_id"] for c in report["cells"]}
    assert cell_ids == set(cells)


def test_load_cell_saturation_uses_the_most_recent_timestamp_per_cell(tmp_path):
    old_dir = tmp_path / "e1-postgres" / "triagem" / "20260101T000000Z"
    new_dir = tmp_path / "e1-postgres" / "triagem" / "20260102T000000Z"
    (old_dir / "rep0").mkdir(parents=True)
    (new_dir / "rep0").mkdir(parents=True)
    (old_dir / "saturation.json").write_text(
        json.dumps({"approx_throughput": 1000.0, "censored": False, "lower_bound": None, "loadgen_bottleneck": False})
    )
    (new_dir / "saturation.json").write_text(
        json.dumps({"approx_throughput": 9000.0, "censored": False, "lower_bound": None, "loadgen_bottleneck": False})
    )

    result = load_cell_saturation([old_dir / "rep0", new_dir / "rep0"])

    assert result["e1-postgres"]["approx_throughput"] == 9000.0


def test_build_report_includes_saturation_fields_and_pareto_frontier(tmp_path):
    results_root, cells = _build_fake_results(tmp_path)
    rep_dirs = discover_rep_dirs(results_root, "triagem")
    ensure_collected(rep_dirs)
    groups = load_cell_latencies(rep_dirs)

    saturation_by_cell = {
        "e1-postgres": {"approx_throughput": 5000.0, "censored": False, "lower_bound": None},
        "e2-postgres": {"approx_throughput": 5000.0, "censored": False, "lower_bound": None},
        "e1-valkey": {"approx_throughput": None, "censored": True, "lower_bound": 50000.0},
    }
    report = build_report(groups, saturation_by_cell)

    by_id = {c["cell_id"]: c for c in report["cells"]}
    assert by_id["e1-postgres"]["saturation_throughput_approx"] == 5000.0
    assert by_id["e1-valkey"]["saturation_censored"] is True
    # e1-valkey tem latência muito pior (deslocada de propósito) — mesmo
    # censurada (vencendo em vazão), não domina em latência/custo, então
    # não deveria varrer a fronteira sozinha.
    assert "pareto_frontier" in report
    assert "censorship_warning" in report
