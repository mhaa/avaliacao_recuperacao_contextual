from __future__ import annotations

import csv
import json

import numpy as np
import polars as pl
import pytest

from analysis.report import discover_rep_dirs
from analysis.stats import vargha_delaney_a
from analysis.stratified_report import (
    build_stratified_report,
    compare_stratum,
    load_strata,
    write_tables,
)


def _write_rep(root, cell_id, rate, tier, latencies, rep=0):
    """Repetição de confirmação já coletada: discover_rep_dirs só precisa de
    requests.ndjson existir, e load_strata lê manifest + latencies.parquet
    (ensure_collected é chamado só por main, fora destes testes)."""
    rep_dir = root / cell_id / "confirmacao" / "20260101T000000Z" / f"{rate}-{tier}" / f"rep{rep}"
    rep_dir.mkdir(parents=True)
    (rep_dir / "requests.ndjson").write_text("")
    (rep_dir / "summary.json").write_text("{}")
    (rep_dir / "manifest.json").write_text(
        json.dumps({"cell_id": cell_id, "rate": rate, "selectivity_tier": tier, "k": 20})
    )
    pl.DataFrame({"latency_ms": np.asarray(latencies, dtype=float)}).write_parquet(
        rep_dir / "latencies.parquet"
    )


def test_load_strata_keeps_only_fixed_rates_and_splits_by_tier_and_cell(tmp_path):
    _write_rep(tmp_path, "e3-valkey", 1000, "low", [2.0] * 4)
    _write_rep(tmp_path, "e3-valkey", 1000, "low", [3.0] * 4, rep=1)
    _write_rep(tmp_path, "e4-valkey", 1000, "low", [6.0] * 4)
    _write_rep(tmp_path, "e3-valkey", 100, "high", [1.0] * 4)
    # Patamar "alto" = vazão de saturação da própria célula — nunca entra.
    _write_rep(tmp_path, "e3-valkey", 3195, "low", [99.0] * 4)

    strata = load_strata(discover_rep_dirs(tmp_path, "confirmacao"))

    assert set(strata) == {(1000, "low"), (100, "high")}
    assert strata[(1000, "low")]["e3-valkey"].tolist() == [2.0] * 4 + [3.0] * 4
    assert strata[(1000, "low")]["e4-valkey"].dtype == np.float64
    assert 99.0 not in np.concatenate(list(strata[(1000, "low")].values()))


def test_opposite_effects_per_stratum_are_not_averaged_away():
    """Regressão do caso real e3-valkey × e4-valkey (docs/DESIGN.md, "Recorte
    da comparação de latência na confirmação"): num estrato uma célula é
    muito mais lenta, no outro é a outra. Agregado, Â fica perto de 0,5 e
    esconde as duas diferenças; por estrato, as duas aparecem como grandes
    e em sentidos opostos."""
    rng = np.random.default_rng(0)
    low = {"x": rng.normal(10, 1, 2000), "y": rng.normal(20, 1, 2000)}
    high = {"x": rng.normal(20, 1, 2000), "y": rng.normal(10, 1, 2000)}

    pooled = vargha_delaney_a(
        np.concatenate([low["x"], high["x"]]), np.concatenate([low["y"], high["y"]])
    )
    low_pair = compare_stratum(low)["pairs"]["x|y"]
    high_pair = compare_stratum(high)["pairs"]["x|y"]

    assert pooled == pytest.approx(0.5, abs=0.05)
    assert low_pair["vargha_delaney_a"] < 0.05 and low_pair["magnitude"] == "grande"
    assert high_pair["vargha_delaney_a"] > 0.95 and high_pair["magnitude"] == "grande"


def test_compare_stratum_runs_dunn_only_after_kruskal_rejects():
    rng = np.random.default_rng(1)
    same = {c: rng.normal(10, 1, 300) for c in ("a", "b", "c")}
    shifted = {"a": rng.normal(10, 1, 300), "b": rng.normal(10, 1, 300), "c": rng.normal(15, 1, 300)}

    same_result = compare_stratum(same)
    shifted_result = compare_stratum(shifted)

    assert same_result["kruskal_wallis"]["reject_h0"] is False
    assert all(p["dunn_p_bonferroni"] is None for p in same_result["pairs"].values())
    assert shifted_result["kruskal_wallis"]["reject_h0"] is True
    assert shifted_result["pairs"]["a|c"]["dunn_p_bonferroni"] < 0.05
    assert set(shifted_result["pairs"]) == {"a|b", "a|c", "b|c"}  # não ordenados


def test_build_report_and_tables(tmp_path):
    rng = np.random.default_rng(2)
    strata = {
        (1000, "low"): {"e3-valkey": rng.normal(2, 0.1, 500), "e4-valkey": rng.normal(6, 0.5, 500)},
        # Estrato com uma célula só: tem percentis, mas nada a comparar.
        (100, "high"): {"e3-valkey": rng.normal(2, 0.1, 500)},
    }

    report = build_stratified_report(strata, max_workers=1)
    write_tables(report, tmp_path)

    by_stratum = {(s["rate_rps"], s["selectivity_tier"]): s for s in report}
    low = by_stratum[(1000, "low")]
    tail = low["cells"]["e4-valkey"]
    assert tail["p99_ci95_low_ms"] <= tail["p99_ms"] <= tail["p99_ci95_high_ms"]
    assert low["kruskal_wallis"]["reject_h0"] is True
    assert "kruskal_wallis" not in by_stratum[(100, "high")]

    with (tmp_path / "pares_por_estrato.csv").open(encoding="utf-8") as f:
        pairs = list(csv.DictReader(f))
    assert len(pairs) == 1
    assert (pairs[0]["celula_a"], pairs[0]["celula_b"]) == ("e3-valkey", "e4-valkey")
    assert pairs[0]["magnitude"] == "grande"
    with (tmp_path / "latencia_por_estrato.csv").open(encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 3
    with (tmp_path / "testes_globais_por_estrato.csv").open(encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 1
