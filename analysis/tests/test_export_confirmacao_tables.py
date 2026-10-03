"""Testa analysis/export_confirmacao_tables.py contra uma árvore results/
sintética — mesma disciplina de dados sintéticos de
analysis/tests/test_export_triagem_tables.py, sem depender de medição real."""

from __future__ import annotations

import json

from analysis.export_confirmacao_tables import (
    _aggregate_by_component,
    build_rows,
    combo_time_range,
    group_combo_rep_dirs,
)
from analysis.report import discover_rep_dirs, ensure_collected


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


def _write_fake_rep(rep_dir, latencies, cell_id, tier, rate, rep_index=0) -> None:
    rep_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        _request_line(f"2026-01-01T00:0{2 + rep_index}:{i % 60:02d}.000Z", latency)
        for i, latency in enumerate(latencies)
    ]
    (rep_dir / "requests.ndjson").write_text("\n".join(lines) + "\n")
    (rep_dir / "manifest.json").write_text(
        json.dumps(
            {"cell_id": cell_id, "selectivity_tier": tier, "rate": rate, "repetition": rep_index}
        )
    )


def test_group_combo_rep_dirs_keys_by_cell_tier_rate_from_manifest(tmp_path):
    _write_fake_rep(
        tmp_path / "e1-postgres" / "confirmacao" / "ts" / "100-medium" / "rep0",
        [1.0, 2.0],
        "e1-postgres",
        "medium",
        100,
    )
    _write_fake_rep(
        tmp_path / "e1-postgres" / "confirmacao" / "ts" / "1000-high" / "rep0",
        [3.0, 4.0],
        "e1-postgres",
        "high",
        1000,
    )
    rep_dirs = discover_rep_dirs(tmp_path, "confirmacao")
    ensure_collected(rep_dirs)

    groups = group_combo_rep_dirs(rep_dirs)

    assert set(groups) == {("e1-postgres", "medium", 100), ("e1-postgres", "high", 1000)}


def test_combo_time_range_spans_min_and_max_across_reps(tmp_path):
    _write_fake_rep(
        tmp_path / "e1-postgres" / "confirmacao" / "ts" / "100-medium" / "rep0",
        [1.0, 2.0, 3.0],
        "e1-postgres",
        "medium",
        100,
        rep_index=0,
    )
    _write_fake_rep(
        tmp_path / "e1-postgres" / "confirmacao" / "ts" / "100-medium" / "rep1",
        [4.0, 5.0],
        "e1-postgres",
        "medium",
        100,
        rep_index=1,
    )
    rep_dirs = discover_rep_dirs(tmp_path, "confirmacao")
    ensure_collected(rep_dirs)
    groups = group_combo_rep_dirs(rep_dirs)
    dirs = groups[("e1-postgres", "medium", 100)]

    start, end = combo_time_range(dirs)

    assert start.startswith("2026-01-01T00:02:")
    assert end.startswith("2026-01-01T00:03:")


def test_aggregate_by_component_uses_available_mb_when_given():
    import polars as pl

    df = pl.DataFrame(
        {
            "component": ["database", "database", "service"],
            "cpu_percent": [10.0, 20.0, 5.0],
            "memory_mb": [1000.0, 3000.0, 100.0],
            "memory_available_mb": [9000.0, 7000.0, 9900.0],
        }
    )

    stats = _aggregate_by_component(df, "cpu_percent", "memory_mb", available_col="memory_available_mb")

    assert stats["cpu_banco_pct"] == 15.0
    # database: (1000/10000*100 + 3000/10000*100) / 2 = (10 + 30) / 2 = 20
    assert stats["mem_banco_pct"] == 20.0
    assert stats["cpu_servico_pct"] == 5.0
    assert stats["mem_servico_pct"] == 1.0


def test_aggregate_by_component_empty_dataframe_returns_all_none():
    import polars as pl

    df = pl.DataFrame({"component": [], "cpu_percent": [], "memory_mb": []})

    stats = _aggregate_by_component(df, "cpu_percent", "memory_mb", memory_denominator_mb=1000.0)

    assert stats == {
        "cpu_banco_pct": None,
        "cpu_servico_pct": None,
        "mem_banco_pct": None,
        "mem_servico_pct": None,
    }


def test_build_rows_includes_legacy_row_for_cell_without_rep_dirs(tmp_path):
    # e1-postgres tem um combo normal (medium/100) com dado bruto, e a
    # célula e2-scylla só tem saturation_medium.json (confirmação rodou
    # antes da otimização de confirmação arquivada) — vira uma linha extra
    # via legacy_final_level_p99_samples, sem p50/p95/CPU/Mem.
    _write_fake_rep(
        tmp_path / "e1-postgres" / "confirmacao" / "ts" / "100-medium" / "rep0",
        [1.0, 2.0, 3.0, 4.0, 5.0],
        "e1-postgres",
        "medium",
        100,
    )
    legacy_dir = tmp_path / "e2-scylla" / "confirmacao" / "ts2"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "saturation_medium.json").write_text(
        json.dumps(
            {
                "approx_throughput": 3980.0,
                "censored": False,
                "lower_bound": None,
                "final_level_probes": [
                    {"per_rep_p99_ms": [100.0, 110.0, 120.0, 130.0, 140.0]},
                ],
            }
        )
    )

    rows = build_rows(tmp_path)

    by_cell = {(r["celula"], r["seletividade"]): r for r in rows}
    assert by_cell[("e1-postgres", "medium")]["fonte_latencia"] == "requisicoes_brutas"
    assert by_cell[("e1-postgres", "medium")]["p50_ms"] is not None
    legacy_row = by_cell[("e2-scylla", "medium")]
    assert legacy_row["fonte_latencia"] == "resumo_por_repeticao_legado"
    assert legacy_row["p50_ms"] is None
    assert legacy_row["p99_ms"] == 120.0
    assert legacy_row["cpu_banco_pct"] is None
