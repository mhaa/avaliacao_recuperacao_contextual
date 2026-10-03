"""Comparação de latência entre células da confirmação, por estrato
(seletividade × carga fixa) — docs/DESIGN.md, "Recorte da comparação de
latência na confirmação: por estrato, não por célula".

analysis/report.py agrega todas as requisições de uma célula num grupo só.
Na confirmação isso mistura o patamar "alto" (vazão de saturação de CADA
célula, diferente entre elas e ~80% das requisições) com os patamares fixos,
e estratos com efeitos em sentidos opostos se anulam no agregado. Aqui cada
estrato (100 ou 1.000 req/s × baixa/média/alta seletividade) recebe o
protocolo estatístico completo de docs/DESIGN.md ("Estatística") sobre
cargas ofertadas idênticas para todas as células.

Uso:
    docker compose run --rm --entrypoint python tools analysis/stratified_report.py \\
        results --out results/report/confirmacao_estratificada
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import polars as pl

from analysis.report import discover_rep_dirs, ensure_collected, percentiles_of
from analysis.stats import (
    bootstrap_percentile_ci,
    dunn_posthoc,
    effect_size_epsilon_squared,
    kruskal_wallis,
    vargha_delaney_a,
    vargha_delaney_magnitude,
)

# Os únicos patamares de carga idênticos para todas as células da
# confirmação — o terceiro ("alto") é a vazão de saturação de cada célula
# naquela seletividade e fica de fora de propósito (docs/DESIGN.md).
FIXED_RATES = (100, 1000)

# Semente fixa do bootstrap: o IC publicado na dissertação precisa sair
# igual a cada reexecução, não variar na última casa a cada rodada.
BOOTSTRAP_SEED = 0

Stratum = tuple[int, str]  # (taxa em req/s, seletividade)


def load_strata(
    rep_dirs: list[Path], rates: tuple[int, ...] = FIXED_RATES
) -> dict[Stratum, dict[str, np.ndarray]]:
    """Agrupa latency_ms por (taxa, seletividade) e, dentro de cada estrato,
    por célula. Taxa, seletividade e célula vêm do manifest.json de cada
    repetição (mesma disciplina de analysis/report.py:load_cell_latencies);
    repetições fora de `rates` são ignoradas. Arrays float64, não listas —
    mesmo motivo de memória documentado em load_cell_latencies."""
    chunks: dict[Stratum, dict[str, list[np.ndarray]]] = {}
    for rep_dir in rep_dirs:
        manifest = json.loads((rep_dir / "manifest.json").read_text())
        rate = manifest.get("rate")
        if rate not in rates:
            continue
        stratum = (int(rate), manifest["selectivity_tier"])
        latencies = pl.read_parquet(rep_dir / "latencies.parquet")["latency_ms"].to_numpy()
        chunks.setdefault(stratum, {}).setdefault(manifest["cell_id"], []).append(
            latencies.astype(np.float64, copy=False)
        )
    return {
        stratum: {cell_id: np.concatenate(parts) for cell_id, parts in sorted(cells.items())}
        for stratum, cells in sorted(chunks.items())
    }


def compare_stratum(groups: dict[str, np.ndarray]) -> dict:
    """Kruskal-Wallis + ε² e, se rejeitar H0, Dunn/Bonferroni; Â de
    Vargha-Delaney para cada par não ordenado (a < b), sempre — não é teste
    de hipótese, é a magnitude que Dunn não dá (analysis/stats.py).
    Â(a, b) > 0,5 ⇒ `a` tende a ser mais lenta."""
    labels = list(groups)
    kruskal = kruskal_wallis([groups[label] for label in labels])
    n_total = sum(int(groups[label].size) for label in labels)
    dunn = dunn_posthoc(groups) if kruskal.reject_h0 else {}
    pairs = {}
    for i, a in enumerate(labels):
        for b in labels[i + 1 :]:
            a12 = vargha_delaney_a(groups[a], groups[b])
            pairs[f"{a}|{b}"] = {
                "vargha_delaney_a": a12,
                "magnitude": vargha_delaney_magnitude(a12),
                "dunn_p_bonferroni": dunn.get((a, b)),
            }
    return {
        "n_total": n_total,
        "kruskal_wallis": {
            "h_statistic": kruskal.h_statistic,
            "p_value": kruskal.p_value,
            "reject_h0": kruskal.reject_h0,
        },
        "epsilon_squared": effect_size_epsilon_squared(kruskal.h_statistic, n_total, len(labels)),
        "pairs": pairs,
    }


def cell_tail(latencies: np.ndarray) -> dict:
    """Percentis e IC bootstrap do p99 de uma célula num estrato. Nível de
    módulo (não closure) porque ProcessPoolExecutor precisa dar pickle."""
    percentiles = percentiles_of(latencies)
    ci = bootstrap_percentile_ci(latencies, percentile=0.99, seed=BOOTSTRAP_SEED)
    return {
        "n": int(latencies.size),
        "p50_ms": percentiles["p50"],
        "p95_ms": percentiles["p95"],
        "p99_ms": percentiles["p99"],
        "p999_ms": percentiles["p999"],
        "p99_ci95_low_ms": ci.low,
        "p99_ci95_high_ms": ci.high,
    }


def _all_cell_tails(
    strata: dict[Stratum, dict[str, np.ndarray]], max_workers: int
) -> dict[tuple[Stratum, str], dict]:
    """O bootstrap domina o tempo — paralelo por (estrato, célula). Com
    max_workers=1 roda no próprio processo, sem pool (testes, e máquinas
    onde cada worker extra não cabe na memória)."""
    jobs = [(stratum, cell_id) for stratum, cells in strata.items() for cell_id in cells]
    if max_workers == 1:
        return {job: cell_tail(strata[job[0]][job[1]]) for job in jobs}
    tails = {}
    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(cell_tail, strata[stratum][cell_id]): (stratum, cell_id)
            for stratum, cell_id in jobs
        }
        for future in as_completed(futures):
            stratum, cell_id = futures[future]
            tails[(stratum, cell_id)] = future.result()
            print(f"[{stratum[0]} req/s, {stratum[1]}] {cell_id}: p99 concluído", flush=True)
    return tails


def build_stratified_report(
    strata: dict[Stratum, dict[str, np.ndarray]], max_workers: int = 1
) -> list[dict]:
    tails = _all_cell_tails(strata, max_workers)
    report = []
    for (rate, tier), groups in strata.items():
        entry = {"rate_rps": rate, "selectivity_tier": tier, "cells": {}}
        for cell_id in groups:
            entry["cells"][cell_id] = tails[((rate, tier), cell_id)]
        if len(groups) >= 2:
            entry.update(compare_stratum(groups))
        report.append(entry)
    return report


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def write_tables(report: list[dict], out_dir: Path) -> None:
    """Três tabelas planas — uma linha por estrato, por (estrato, célula) e
    por (estrato, par) — no formato que a dissertação consome."""
    _write_csv(
        out_dir / "testes_globais_por_estrato.csv",
        ["carga_rps", "seletividade", "n_total", "kruskal_h", "kruskal_p", "rejeita_h0", "epsilon2"],
        [
            [
                s["rate_rps"], s["selectivity_tier"], s["n_total"],
                s["kruskal_wallis"]["h_statistic"], s["kruskal_wallis"]["p_value"],
                s["kruskal_wallis"]["reject_h0"], s["epsilon_squared"],
            ]
            for s in report
            if "kruskal_wallis" in s
        ],
    )
    _write_csv(
        out_dir / "latencia_por_estrato.csv",
        ["carga_rps", "seletividade", "celula", "n", "p50_ms", "p95_ms", "p99_ms", "p999_ms",
         "p99_ic95_baixo_ms", "p99_ic95_alto_ms"],
        [
            [
                s["rate_rps"], s["selectivity_tier"], cell_id, t["n"], t["p50_ms"], t["p95_ms"],
                t["p99_ms"], t["p999_ms"], t["p99_ci95_low_ms"], t["p99_ci95_high_ms"],
            ]
            for s in report
            for cell_id, t in s["cells"].items()
        ],
    )
    _write_csv(
        out_dir / "pares_por_estrato.csv",
        ["carga_rps", "seletividade", "celula_a", "celula_b", "dunn_p_bonferroni",
         "vargha_delaney_a", "magnitude"],
        [
            [
                s["rate_rps"], s["selectivity_tier"], *pair.split("|"),
                p["dunn_p_bonferroni"], p["vargha_delaney_a"], p["magnitude"],
            ]
            for s in report
            for pair, p in s.get("pairs", {}).items()
        ],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_root", type=Path)
    parser.add_argument("--out", type=Path, default=Path("results/report/confirmacao_estratificada"))
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="processos em paralelo no bootstrap (default: nº de CPUs; 1 = sem pool)",
    )
    args = parser.parse_args(argv)

    rep_dirs = discover_rep_dirs(args.results_root, "confirmacao")
    if not rep_dirs:
        print(f"Nenhum resultado de confirmação em {args.results_root}.")
        return 1
    ensure_collected(rep_dirs)

    strata = load_strata(rep_dirs)
    for (rate, tier), cells in strata.items():
        sizes = ", ".join(f"{c}={g.size}" for c, g in cells.items())
        print(f"Estrato {rate} req/s, {tier}: {sizes}", flush=True)

    max_workers = max(1, args.max_workers or os.cpu_count() or 1)
    report = build_stratified_report(strata, max_workers=max_workers)

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "stratified.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    write_tables(report, args.out)
    print(f"Relatório estratificado escrito em {args.out} ({len(report)} estratos).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
