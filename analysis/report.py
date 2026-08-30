"""Orquestra a análise em lote sobre `results/` — consolida as repetições de
várias células (via analysis/collect.py), roda a estatística exigida por
CONTEXTO.md ("Estatística" / "Delineamento em duas etapas") e gera os
gráficos de analysis/plots.py. Antes deste script, `collect.py`/`stats.py`/
`plots.py` só eram chamados um `run_dir` (ou uma síntese) de cada vez, sem
nada consolidando as 14 células (triagem) ou a fronteira de Pareto
(confirmação) num resultado só.

Uso:
    docker compose run --rm --entrypoint python tools analysis/report.py \\
        results --phase triagem --out results/report/triagem
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import polars as pl

from analysis.collect import collect
from analysis.plots import plot_pareto_frontier, plot_percentile_comparison
from analysis.stats import (
    bootstrap_percentile_ci,
    dunn_posthoc,
    effect_size_epsilon_squared,
    kruskal_wallis,
    tost_equivalence,
)


def storage_for_cell(cell_id: str) -> str:
    """Mesma convenção de infra/scripts/cloud_smoke_test.py:storage_for_cell
    (cells/<id>.yaml sempre e<1-4>-<storage>) — não importada de lá para
    evitar um import cruzado analysis/infra que quebraria quando este
    script roda como arquivo direto (`python analysis/report.py`): infra/
    é bind-mount-only, nunca copiado pela imagem (docker-compose.yml), e só
    é resolvível por nome absoluto quando /app já está no sys.path."""
    return cell_id.split("-", 1)[1]

# Preços públicos on-demand do GCP, us-central1 (checar/atualizar ao usar de
# verdade — placeholder até dimensionamento.xlsx, Etapa 1, dar custo real
# por célula). Hoje todo storage usa os mesmos tipos de máquina
# (infra/modules/database e /service têm o mesmo default para as 4), então
# o custo sai igual nas 4 — limitação conhecida do placeholder, não um bug.
MACHINE_HOURLY_USD = {"n2-standard-4": 0.194, "n2-standard-8": 0.388}
CELL_COST_USD_HOUR = {
    storage: MACHINE_HOURLY_USD["n2-standard-8"] + MACHINE_HOURLY_USD["n2-standard-4"]
    for storage in ("postgres", "valkey", "scylla", "opensearch")
}

# Margem de equivalência prática para o TOST na confirmação — placeholder,
# confirmar com o usuário o valor real antes de usar num resultado do TCC.
EQUIVALENCE_MARGIN_MS = 10.0


def discover_rep_dirs(results_root: Path, phase: str) -> list[Path]:
    return sorted(
        p.parent
        for p in results_root.glob(f"*/{phase}/*/rep*/k6-raw.json")
    )


def ensure_collected(rep_dirs: list[Path]) -> None:
    for rep_dir in rep_dirs:
        if not (rep_dir / "summary.json").exists():
            collect(rep_dir)


def load_cell_latencies(rep_dirs: list[Path]) -> dict[str, list[float]]:
    """Agrupa por cell_id (results/<cell_id>/<phase>/<timestamp>/rep<N>/),
    concatenando latency_ms de todas as repetições daquela célula."""
    by_cell: dict[str, list[float]] = {}
    for rep_dir in rep_dirs:
        cell_id = rep_dir.parents[2].name
        df = pl.read_parquet(rep_dir / "latencies.parquet")
        by_cell.setdefault(cell_id, []).extend(df["latency_ms"].to_list())
    return by_cell


def percentiles_of(latencies: list[float]) -> dict[str, float]:
    arr = np.asarray(latencies, dtype=float)
    return {
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "p99": float(np.percentile(arr, 99)),
        "p999": float(np.percentile(arr, 99.9)),
    }


def build_report(groups: dict[str, list[float]]) -> dict:
    labels = list(groups)
    kruskal = kruskal_wallis([groups[label] for label in labels])
    dunn = dunn_posthoc(groups) if kruskal.reject_h0 else {}
    n_total = sum(len(v) for v in groups.values())
    epsilon_squared = effect_size_epsilon_squared(kruskal.h_statistic, n_total, len(labels))

    cells = []
    bootstrap_ci_p99 = {}
    for cell_id, latencies in groups.items():
        p99 = percentiles_of(latencies)["p99"]
        cells.append(
            {
                "cell_id": cell_id,
                "latency_p99_ms": p99,
                "cost_usd_hour": CELL_COST_USD_HOUR[storage_for_cell(cell_id)],
            }
        )
        ci = bootstrap_percentile_ci(latencies, percentile=0.99)
        bootstrap_ci_p99[cell_id] = {"low": ci.low, "high": ci.high}

    return {
        "kruskal_wallis": {
            "h_statistic": kruskal.h_statistic,
            "p_value": kruskal.p_value,
            "reject_h0": kruskal.reject_h0,
        },
        "dunn_posthoc": {f"{a}|{b}": p for (a, b), p in dunn.items()},
        "effect_size_epsilon_squared": epsilon_squared,
        "bootstrap_ci_p99": bootstrap_ci_p99,
        "cells": cells,
    }


def build_confirmation_extras(groups: dict[str, list[float]]) -> dict:
    """TOST par-a-par entre as células da fronteira — 'não rejeitou H0' não
    é o mesmo que 'equivalente na prática' (CONTEXTO.md, "Delineamento em
    duas etapas")."""
    labels = list(groups)
    tost_by_pair = {}
    for i, a in enumerate(labels):
        for b in labels[i + 1 :]:
            result = tost_equivalence(
                groups[a], groups[b], -EQUIVALENCE_MARGIN_MS, EQUIVALENCE_MARGIN_MS
            )
            tost_by_pair[f"{a}|{b}"] = {
                "equivalent": result.equivalent,
                "p_greater": result.p_greater,
                "p_less": result.p_less,
            }
    return {"equivalence_margin_ms": EQUIVALENCE_MARGIN_MS, "tost_by_pair": tost_by_pair}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_root", type=Path)
    parser.add_argument("--phase", required=True, choices=["triagem", "confirmacao"])
    parser.add_argument("--out", type=Path, default=Path("results/report"))
    args = parser.parse_args(argv)

    rep_dirs = discover_rep_dirs(args.results_root, args.phase)
    if not rep_dirs:
        print(f"Nenhum resultado encontrado em {args.results_root} para a fase {args.phase}.")
        return 1

    ensure_collected(rep_dirs)
    groups = load_cell_latencies(rep_dirs)

    report = build_report(groups)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(report, indent=2))
    plot_pareto_frontier(report["cells"], args.out / "pareto.png")

    if args.phase == "confirmacao":
        extra = build_confirmation_extras(groups)
        (args.out / "confirmacao_extra.json").write_text(json.dumps(extra, indent=2))
        percentiles_by_cell = {cell_id: percentiles_of(v) for cell_id, v in groups.items()}
        plot_percentile_comparison(percentiles_by_cell, args.out / "percentile_comparison.png")

    print(f"Relatório escrito em {args.out} ({len(groups)} células, {len(rep_dirs)} repetições).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
