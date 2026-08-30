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
)


def _point(metric: str, time: str, value: float, tags: dict) -> str:
    return json.dumps(
        {"metric": metric, "type": "Point", "data": {"time": time, "value": value, "tags": tags}}
    )


def _write_fake_run(rep_dir, latencies: list[float]) -> None:
    rep_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, latency in enumerate(latencies):
        request_id = f"{i}-0"
        time = f"2026-01-01T00:02:{i % 60:02d}.000Z"
        lines.append(
            _point(
                "http_req_duration",
                time,
                latency,
                {"scenario": "measurement", "status": "200", "request_id": request_id},
            )
        )
        lines.append(
            _point("returned_count", time, 20, {"scenario": "measurement", "request_id": request_id})
        )
    (rep_dir / "k6-raw.json").write_text("\n".join(lines) + "\n")


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
