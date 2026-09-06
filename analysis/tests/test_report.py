"""Testa analysis/report.py contra uma árvore results/ sintética (3 células
fictícias, uma com latência deslocada de propósito) — mesma disciplina de
dados sintéticos com propriedade conhecida de analysis/tests/test_stats.py,
e o mesmo formato de NDJSON de analysis/tests/test_collect.py, sem depender
de k6 nem de uma medição real."""

from __future__ import annotations

import json

import numpy as np
import pytest

from analysis.report import (
    DISK_USD_PER_GB_MONTH,
    build_report,
    discover_rep_dirs,
    ensure_collected,
    load_cell_latencies,
    load_cell_saturation,
    storage_medium_for_cell,
    unit_storage_cost_usd_month,
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


def _write_fake_storage_sizes(storage_root):
    """Fixture sintética pra storage_bytes_for_cell/unit_storage_cost_usd_month
    (analysis/report.py) — mesmo formato que
    infra/scripts/measure_storage_size.py grava de verdade em
    results/storage/<storage>.json, um valor plausível por
    tabela/padrão/índice usado pelas células e1/e2-postgres e e1-valkey
    (as únicas que os testes deste arquivo exercitam)."""
    storage_root.mkdir(parents=True, exist_ok=True)
    (storage_root / "postgres.json").write_text(
        json.dumps(
            {
                "backend": "postgres",
                "sizes": {
                    "candidates": 1000,
                    "item_contexts": 500,
                    "prematerialized": 300,
                    "inverted_lists": 100,
                },
            }
        )
    )
    (storage_root / "valkey.json").write_text(
        json.dumps(
            {
                "backend": "valkey",
                "sizes": {
                    "candidates:*": {"key_count": 10, "sampled": 10, "bytes_estimate": 2000},
                    "item_contexts:*": {"key_count": 10, "sampled": 10, "bytes_estimate": 500},
                    "candidates_set:*": {"key_count": 10, "sampled": 10, "bytes_estimate": 1500},
                    "inverted:*": {"key_count": 10, "sampled": 10, "bytes_estimate": 400},
                    "prematerialized:*": {"key_count": 10, "sampled": 10, "bytes_estimate": 300},
                },
            }
        )
    )


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
    storage_root = tmp_path / "storage"
    _write_fake_storage_sizes(storage_root)

    report = build_report(groups, storage_root=storage_root)

    assert report["kruskal_wallis"]["reject_h0"] is True
    assert report["dunn_posthoc"]["e1-postgres|e1-valkey"] < 0.05
    assert report["dunn_posthoc"]["e2-postgres|e1-valkey"] < 0.05
    assert report["dunn_posthoc"]["e1-postgres|e2-postgres"] > 0.05
    cell_ids = {c["cell_id"] for c in report["cells"]}
    assert cell_ids == set(cells)

    # Sem saturação nenhuma, NENHUMA célula pode ser posta no plano de custo
    # (n(D) é indeterminado). Isso não pode explodir nem passar silencioso: a
    # estatística continua válida, o custo simplesmente não existe, e o
    # relatório tem de dizer isso em voz alta.
    assert all(c["cost_defined"] is False for c in report["cells"])
    assert all(seg["pareto_frontier"] == [] for seg in report["frontier_segments"])
    assert report["pareto_frontier_union"] == []
    assert {c["cell_id"] for c in report["cells_without_cost"]} == set(cells)
    assert report["cost_model_warnings"]


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

    result = load_cell_saturation(tmp_path, "triagem")

    assert result["e1-postgres"]["approx_throughput"] == 9000.0


def test_load_cell_saturation_finds_a_ramp_only_run_that_has_no_repetitions(tmp_path):
    """Regressão do caminho `--only-saturation`: a rampa é re-executada
    sozinha e grava um timestamp NOVO contendo só saturation.json, sem
    `rep*/`. Enquanto esta função derivava o diretório dos rep_dirs, esse
    arquivo novo era invisível e o relatório seguia usando o S antigo — sem
    aviso nenhum, com todo o custo calculado sobre o valor errado."""
    with_reps = tmp_path / "e1-postgres" / "triagem" / "20260101T000000Z"
    (with_reps / "rep0").mkdir(parents=True)
    (with_reps / "saturation.json").write_text(
        json.dumps({"approx_throughput": 1000.0, "censored": False, "lower_bound": None})
    )
    ramp_only = tmp_path / "e1-postgres" / "triagem" / "20260202T000000Z"
    ramp_only.mkdir(parents=True)
    (ramp_only / "saturation.json").write_text(
        json.dumps({"approx_throughput": 1750.0, "censored": False, "lower_bound": None})
    )

    result = load_cell_saturation(tmp_path, "triagem")

    assert result["e1-postgres"]["approx_throughput"] == 1750.0


def test_build_report_emits_demand_levels_segments_and_crossovers(tmp_path):
    results_root, cells = _build_fake_results(tmp_path)
    rep_dirs = discover_rep_dirs(results_root, "triagem")
    ensure_collected(rep_dirs)
    groups = load_cell_latencies(rep_dirs)

    saturation_by_cell = {
        "e1-postgres": {"approx_throughput": 5000.0, "censored": False, "lower_bound": None},
        "e2-postgres": {"approx_throughput": 5000.0, "censored": False, "lower_bound": None},
        "e1-valkey": {"approx_throughput": None, "censored": True, "lower_bound": 50000.0},
    }
    storage_root = tmp_path / "storage"
    _write_fake_storage_sizes(storage_root)
    report = build_report(groups, saturation_by_cell, storage_root=storage_root)

    by_id = {c["cell_id"]: c for c in report["cells"]}
    assert by_id["e1-postgres"]["saturation_throughput_approx"] == 5000.0
    assert by_id["e1-valkey"]["saturation_censored"] is True
    # Censurada: n = 1 exato em todo o domínio medido, nunca o teto como S.
    assert by_id["e1-valkey"]["cost_defined"] is True

    assert [level["demand_rps"] for level in report["demand_levels"]] == [100.0, 1000.0, 10000.0]

    segments = report["frontier_segments"]
    assert segments[0]["demand_from_rps_exclusive"] == 0.0
    # Domínio limitado ao lower_bound da célula censurada.
    assert segments[-1]["demand_to_rps_inclusive"] == 50000.0
    for previous, current in zip(segments, segments[1:]):
        assert previous["demand_to_rps_inclusive"] == current["demand_from_rps_exclusive"]

    assert set(report["crossovers"]) == {"frontier", "cost"}
    union = {cid for seg in segments for cid in seg["pareto_frontier"]}
    assert set(report["pareto_frontier_union"]) == union
    assert "censorship_warning" in report


def test_valkey_storage_goes_to_the_capacity_term_and_the_others_to_the_disk_parcel(tmp_path):
    """A única linha que faz o armazenamento discriminar as tecnologias — e
    que estava sem cobertura nenhuma."""
    storage_root = tmp_path / "storage"
    _write_fake_storage_sizes(storage_root)

    assert storage_medium_for_cell("e1-valkey") == "memory"
    assert storage_medium_for_cell("e1-postgres") == "disk"
    # Memória não tem preço por GiB: age via n(D) (⌈V_mem/M⌉), não via C_a.
    # Zero aqui NÃO quer dizer "armazenamento de graça".
    assert unit_storage_cost_usd_month("e1-valkey", storage_root) == 0.0
    assert unit_storage_cost_usd_month("e1-postgres", storage_root) > 0.0


def test_disk_parcel_is_monthly_not_hourly(tmp_path):
    """Trava a correção horário -> mensal: 10 GiB a US$ 0,187/GiB-mês são
    ~US$ 1,87/mês. Com a constante horária antiga davam ~US$ 0,0026, e a
    parcela de estoque sumia dentro do arredondamento do custo de computação."""
    storage_root = tmp_path / "storage"
    storage_root.mkdir(parents=True, exist_ok=True)
    (storage_root / "postgres.json").write_text(
        json.dumps({"backend": "postgres", "sizes": {"candidates": 10 * 1024**3}})
    )

    cost = unit_storage_cost_usd_month("e1-postgres", storage_root)

    assert cost == pytest.approx(10 * DISK_USD_PER_GB_MONTH)
    assert cost == pytest.approx(1.87, rel=1e-3)
