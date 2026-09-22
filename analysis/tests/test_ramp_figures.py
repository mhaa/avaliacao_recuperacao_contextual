"""Testes de analysis/ramp_figures.py — montagem das séries e geração das
três figuras a partir dos artefatos da campanha. Tudo sintético em tmp_path."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone

from analysis.ramp_figures import (
    DB_AGGREGATE_LABEL,
    db_max_core_series,
    generate,
    resource_series,
)
from analysis.ramp_resources import step_windows

T0 = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)


def _at(seconds):
    return T0 + timedelta(seconds=seconds)


def _step(rate, phase, start_s, end_s, p99=100.0, ok=True):
    return {
        "rate": rate,
        "phase": phase,
        "throughput_rps": float(rate),
        "latency_ms_p50": 2.0,
        "latency_ms_p95": 3.0,
        "latency_ms_p99": p99,
        "latency_ms_p999": p99 * 2,
        "offered_load_ok": ok,
        "started_at": _at(start_s).isoformat(),
        "ended_at": _at(end_s).isoformat(),
    }


def _steps():
    return [
        _step(1000, "fine_up", 0, 60),
        _step(2000, "fine_up", 61, 120, p99=900.0, ok=False),
        _step(1000, "fine_down", 200, 260, p99=800.0),
    ]


def _write_ramp_json(path, steps, cell="e3-valkey"):
    path.write_text(
        json.dumps(
            {
                "cell_id": cell,
                "tier": "medium",
                "steps": steps,
                "recovery": {"verdict": "lost", "reason": None, "comparisons": []},
            }
        )
    )


def _write_resources_csv(path, rows):
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "component",
                "timestamp",
                "cpu_percent",
                "memory_mb",
                "network_mbps",
                "memory_available_mb",
            ]
        )
        writer.writerows(rows)


def _write_db_cpu_csv(path, rows):
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "core", "cpu_percent"])
        writer.writerows(rows)


def test_database_cpu_is_relabelled_so_the_two_curves_can_coexist(tmp_path):
    # A figura precisa mostrar agregado E máximo por núcleo lado a lado; se
    # as duas séries usassem o rótulo "database", uma sobrescreveria a outra.
    path = tmp_path / "resources.csv"
    _write_resources_csv(path, [["database", _at(30).isoformat(), "12.5", "20000", "", ""]])

    cpu, _ = resource_series(path, step_windows(_steps()))

    assert DB_AGGREGATE_LABEL in cpu
    assert "database" not in cpu


def test_samples_outside_any_step_are_ignored(tmp_path):
    # Caem entre degraus ou no setup/teardown — atribuí-las inventaria dado.
    path = tmp_path / "resources.csv"
    _write_resources_csv(
        path,
        [
            ["service", _at(30).isoformat(), "40.0", "1000", "", ""],
            ["service", _at(150).isoformat(), "99.0", "1000", "", ""],
        ],
    )

    cpu, _ = resource_series(path, step_windows(_steps()))

    assert cpu["service"] == [(1000.0, 40.0)]


def test_db_max_core_takes_the_peak_not_the_mean(tmp_path):
    # Média entre núcleos esconderia de novo a thread única do Valkey — que
    # é exatamente o que esta série existe para revelar.
    path = tmp_path / "db_cpu_cores.csv"
    _write_db_cpu_csv(
        path,
        [
            [_at(30).isoformat(), "cpu0", "100.0"],
            [_at(30).isoformat(), "cpu1", "0.0"],
            [_at(30).isoformat(), "cpu2", "0.0"],
        ],
    )

    assert db_max_core_series(path, step_windows(_steps())) == [(1000.0, 100.0)]


def test_db_max_core_excludes_the_aggregate_line(tmp_path):
    path = tmp_path / "db_cpu_cores.csv"
    _write_db_cpu_csv(
        path,
        [
            [_at(30).isoformat(), "cpu", "12.5"],
            [_at(30).isoformat(), "cpu0", "8.0"],
        ],
    )

    assert db_max_core_series(path, step_windows(_steps())) == [(1000.0, 8.0)]


def test_generate_writes_all_three_figures(tmp_path):
    ramp = tmp_path / "ramp_medium.json"
    _write_ramp_json(ramp, _steps())
    resources = tmp_path / "resources.csv"
    _write_resources_csv(resources, [["database", _at(30).isoformat(), "12.5", "20000", "", ""]])
    db_cpu = tmp_path / "db_cpu_cores.csv"
    _write_db_cpu_csv(db_cpu, [[_at(30).isoformat(), "cpu0", "99.0"]])

    written = generate(ramp, tmp_path / "figs", resources, db_cpu)

    assert len(written) == 3
    assert all(p.exists() for p in written)


def test_latency_figures_survive_missing_telemetry(tmp_path):
    # Uma execução abortada cedo pode não ter resources.csv; as figuras que
    # só dependem do JSON precisam sair mesmo assim.
    ramp = tmp_path / "ramp_medium.json"
    _write_ramp_json(ramp, _steps())

    written = generate(ramp, tmp_path / "figs", tmp_path / "ausente.csv", tmp_path / "sumiu.csv")

    assert len(written) == 2
    assert all(p.exists() for p in written)


def test_figure_names_carry_the_cell_so_cells_do_not_overwrite_each_other(tmp_path):
    for cell in ("e2-scylla", "e3-valkey"):
        ramp = tmp_path / f"ramp_{cell}.json"
        _write_ramp_json(ramp, _steps(), cell=cell)
        generate(ramp, tmp_path / "figs")

    produced = {p.name for p in (tmp_path / "figs").iterdir()}
    assert "ramp_percentiles_e2-scylla.png" in produced
    assert "ramp_percentiles_e3-valkey.png" in produced


def test_series_follow_ramp_trajectory_order_not_rate_order(tmp_path):
    # A mesma taxa aparece na subida e na descida; ordenar por taxa fundiria
    # os dois lados da histerese.
    path = tmp_path / "resources.csv"
    _write_resources_csv(
        path,
        [
            ["service", _at(30).isoformat(), "10.0", "1000", "", ""],
            ["service", _at(90).isoformat(), "80.0", "1000", "", ""],
            ["service", _at(230).isoformat(), "20.0", "1000", "", ""],
        ],
    )

    cpu, _ = resource_series(path, step_windows(_steps()))

    assert cpu["service"] == [(1000.0, 10.0), (2000.0, 80.0), (1000.0, 20.0)]
