"""Exporta a tabela de confirmação por COMBO (célula × seletividade × carga)
em CSV — uma linha por (cell_id, tier, rate) com p50/p95/p99, IC 95% do p99 e
CPU/memória médios do banco e do serviço durante aquele combo.

Fora do fluxo de analysis/report.py de propósito: report.py pool TODOS os
combos de uma célula num p99 só (load_cell_latencies), que é o que
report.json precisa para custo/Pareto por célula — não a granularidade por
carga que esta tabela expõe.

Duas fontes de CPU/memória coexistem (results/<cell>/confirmacao/<ts>/):
- resources_recovered.csv (campanha original, reconstruído do Cloud
  Monitoring pós-fato): já vem por (tier, rate, probe), cobre só as
  sondagens da rampa de saturação (não os patamares fixos 100/1000).
- resources.csv (campanha desta sessão, coleta ao vivo): component,
  timestamp, cpu_percent, memory_mb, memory_available_mb — sem tier/rate,
  precisa ser janelado pelo intervalo real de timestamps das requisições do
  combo (latencies.parquet).

Uso:
    python -m analysis.export_confirmacao_tables results \\
        --out docs/resultados_confirmacao
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import polars as pl

from analysis.report import (
    discover_rep_dirs,
    ensure_collected,
    legacy_final_level_p99_samples,
    load_cell_saturation,
    percentiles_of,
)
from analysis.stats import bootstrap_percentile_ci

# n2-standard-8 (infra/modules/database, infra/modules/service): 4 GiB/vCPU
# × 8 vCPU — denominador de memory_mb quando a amostra não carrega o próprio
# teto (resources_recovered.csv, formato da campanha original).
MACHINE_MEMORY_MB = 8 * 4 * 1024

HEADER = [
    "celula",
    "seletividade",
    "carga_rps",
    "n_requisicoes",
    "p50_ms",
    "p95_ms",
    "p99_ms",
    "ic95_p99_baixo_ms",
    "ic95_p99_alto_ms",
    "cpu_banco_pct",
    "cpu_servico_pct",
    "mem_banco_pct",
    "mem_servico_pct",
    "fonte_latencia",
]


def _manifest(rep_dir: Path) -> dict:
    return json.loads((rep_dir / "manifest.json").read_text())


def group_combo_rep_dirs(rep_dirs: list[Path]) -> dict[tuple[str, str, int], list[Path]]:
    """Agrupa rep_dirs por (cell_id, tier, rate) lidos do manifest.json de
    cada repetição — não do nome do diretório: load/run_battery.py usa
    round(rate) no nome (combo_out_dir), o manifest é a fonte exata."""
    groups: dict[tuple[str, str, int], list[Path]] = {}
    for rep_dir in rep_dirs:
        m = _manifest(rep_dir)
        key = (m["cell_id"], m["selectivity_tier"], m["rate"])
        groups.setdefault(key, []).append(rep_dir)
    return groups


def combo_latencies(rep_dirs: list[Path]) -> list[float]:
    latencies: list[float] = []
    for rep_dir in rep_dirs:
        df = pl.read_parquet(rep_dir / "latencies.parquet")
        latencies.extend(df["latency_ms"].to_list())
    return latencies


def combo_time_range(rep_dirs: list[Path]) -> tuple[str, str] | None:
    """[min, max] timestamp ISO-8601 das requisições do combo (todas as
    reps) — janela real usada para filtrar resources.csv (formato novo,
    sem tier/rate próprios). None se não sobrar nenhuma requisição
    (repetição sem nenhuma medição parseada)."""
    mins, maxs = [], []
    for rep_dir in rep_dirs:
        df = pl.read_parquet(rep_dir / "latencies.parquet")
        if df.is_empty():
            continue
        mins.append(df["timestamp"].min())
        maxs.append(df["timestamp"].max())
    if not mins:
        return None
    return min(mins).isoformat(), max(maxs).isoformat()


def resource_stats_from_recovered_csv(
    csv_path: Path, tier: str, rate: int
) -> dict[str, float | None]:
    """resources_recovered.csv: já vem por (tier, rate, probe) — filtra
    exato, sem janela de tempo. memory_mb vira percentual por
    MACHINE_MEMORY_MB (o arquivo não carrega memory_available_mb, ao
    contrário do resources.csv ao vivo)."""
    df = pl.read_csv(csv_path)
    matched = df.filter((pl.col("tier") == tier) & (pl.col("rate") == rate))
    return _aggregate_by_component(matched, "cpu_percent", "memory_mb", memory_denominator_mb=MACHINE_MEMORY_MB)


def resource_stats_from_live_csv(
    csv_path: Path, time_range: tuple[str, str] | None
) -> dict[str, float | None]:
    """resources.csv ao vivo: sem tier/rate — janela pelo intervalo real de
    timestamps das requisições do combo (combo_time_range). memory_mb vira
    percentual via memory_available_mb da própria amostra (mais exato que
    presumir o tamanho da máquina)."""
    if time_range is None:
        return _empty_resource_stats()
    df = pl.read_csv(csv_path, try_parse_dates=True)
    start, end = time_range
    matched = df.filter(
        (pl.col("timestamp") >= pl.lit(start).str.to_datetime(time_zone="UTC"))
        & (pl.col("timestamp") <= pl.lit(end).str.to_datetime(time_zone="UTC"))
    )
    return _aggregate_by_component(matched, "cpu_percent", "memory_mb", available_col="memory_available_mb")


def _empty_resource_stats() -> dict[str, float | None]:
    return {
        "cpu_banco_pct": None,
        "cpu_servico_pct": None,
        "mem_banco_pct": None,
        "mem_servico_pct": None,
    }


def _aggregate_by_component(
    df: pl.DataFrame,
    cpu_col: str,
    mem_col: str,
    memory_denominator_mb: float | None = None,
    available_col: str | None = None,
) -> dict[str, float | None]:
    if df.is_empty():
        return _empty_resource_stats()

    def _component_means(component: str) -> tuple[float | None, float | None]:
        rows = df.filter(pl.col("component") == component)
        if rows.is_empty():
            return None, None
        cpu = float(rows[cpu_col].mean())
        if available_col is not None:
            mem_pct = float(
                (rows[mem_col] / (rows[mem_col] + rows[available_col]) * 100).mean()
            )
        elif memory_denominator_mb:
            mem_pct = float(rows[mem_col].mean()) / memory_denominator_mb * 100
        else:
            mem_pct = None
        return cpu, mem_pct

    cpu_db, mem_db = _component_means("database")
    cpu_svc, mem_svc = _component_means("service")
    return {
        "cpu_banco_pct": cpu_db,
        "cpu_servico_pct": cpu_svc,
        "mem_banco_pct": mem_db,
        "mem_servico_pct": mem_svc,
    }


def resource_stats_for_combo(
    results_root: Path, cell_id: str, tier: str, rate: int, time_range: tuple[str, str] | None
) -> dict[str, float | None]:
    """Tenta resources_recovered.csv (exato por tier/rate) em qualquer
    timestamp de confirmacao da célula; se não achar nada lá, cai para
    resources.csv (ao vivo, janelado pelo tempo) do timestamp mais recente
    que tiver o arquivo. Nenhuma das duas fontes é garantida existir — uma
    célula sem nenhuma vira None em tudo, nunca inventa um valor."""
    for recovered in sorted((results_root / cell_id / "confirmacao").glob("*/resources_recovered.csv")):
        stats = resource_stats_from_recovered_csv(recovered, tier, rate)
        if any(v is not None for v in stats.values()):
            return stats

    live_csvs = sorted((results_root / cell_id / "confirmacao").glob("*/resources.csv"))
    for live in reversed(live_csvs):
        stats = resource_stats_from_live_csv(live, time_range)
        if any(v is not None for v in stats.values()):
            return stats

    return _empty_resource_stats()


def _compute_combo_row(
    results_root: Path, cell_id: str, tier: str, rate: int, dirs: list[Path]
) -> dict:
    """Trabalho de UM combo — função de módulo (não closure) de propósito:
    ProcessPoolExecutor precisa dar pickle nela e nos argumentos (mesma
    disciplina de analysis/report.py:_compute_cell_report_entry). O
    bootstrap de 10.000 reamostras é single-threaded e domina o tempo em
    combos grandes (até ~7M requisições, 5 repetições) — sequencial pelos
    ~37 combos de uma campanha real levaria horas; paralelizar por combo é
    o que faz report.py fazer o mesmo por célula."""
    started = time.monotonic()
    latencies = combo_latencies(dirs)
    n = len(latencies)
    print(f"[{cell_id}/{tier}/{rate}] iniciando (N={n} requisições)...", flush=True)
    percentiles = percentiles_of(latencies)
    ci = bootstrap_percentile_ci(latencies, percentile=0.99)
    time_range = combo_time_range(dirs)
    resources = resource_stats_for_combo(results_root, cell_id, tier, rate, time_range)
    elapsed = time.monotonic() - started
    print(
        f"[{cell_id}/{tier}/{rate}] concluído em {elapsed:.1f}s (p99={percentiles['p99']:.2f}ms)",
        flush=True,
    )
    return {
        "celula": cell_id,
        "seletividade": tier,
        "carga_rps": rate,
        "n_requisicoes": n,
        "p50_ms": percentiles["p50"],
        "p95_ms": percentiles["p95"],
        "p99_ms": percentiles["p99"],
        "ic95_p99_baixo_ms": ci.low,
        "ic95_p99_alto_ms": ci.high,
        "fonte_latencia": "requisicoes_brutas",
        **resources,
    }


def build_rows(results_root: Path, max_workers: int | None = None) -> list[dict]:
    rep_dirs = discover_rep_dirs(results_root, "confirmacao")
    ensure_collected(rep_dirs)
    combos = group_combo_rep_dirs(rep_dirs)

    # Mesmo teto de analysis/report.py:_resolve_max_workers — cada worker
    # aloca seu próprio lote do bootstrap (analysis/stats.py:
    # _BOOTSTRAP_BATCH_TARGET_BYTES), então o pico de memória agregado
    # escala com max_workers, não com o nº de combos. Achado ao vivo nesta
    # mesma sessão: 3 workers em paralelo sobre combos de milhões de linhas
    # estouraram ArrayMemoryError num host de 16GB.
    resolved_workers = max(1, min(len(combos), os.cpu_count() or 1))
    if max_workers is not None:
        resolved_workers = max(1, min(resolved_workers, max_workers))
    print(
        f"Calculando estatísticas por combo ({len(combos)} combos, até "
        f"{resolved_workers} em paralelo)...",
        flush=True,
    )

    rows = []
    with ProcessPoolExecutor(max_workers=resolved_workers) as pool:
        futures = {
            pool.submit(_compute_combo_row, results_root, cell_id, tier, rate, dirs): (
                cell_id,
                tier,
                rate,
            )
            for (cell_id, tier, rate), dirs in combos.items()
        }
        for future in as_completed(futures):
            rows.append(future.result())

    # Células com saturation_<tier>.json mas SEM rep_dirs para o patamar
    # mais recente (confirmação rodou antes da otimização de confirmação
    # arquivada — ver legacy_final_level_p99_samples): entram como uma
    # linha extra por tier, sem p50/p95 (o formato leve nunca os
    # registrou) e sem CPU/memória (sem manifest.json não há como janelar
    # o intervalo real das repetições).
    saturation_by_cell = load_cell_saturation(results_root, "confirmacao")
    existing_cell_tiers = {(cell_id, tier) for cell_id, tier, _ in combos}
    for cell_id, entry in saturation_by_cell.items():
        for tier, tier_entry in entry.get("by_tier", {}).items():
            if (cell_id, tier) in existing_cell_tiers:
                continue
            samples = legacy_final_level_p99_samples({"by_tier": {tier: tier_entry}})
            if not samples:
                continue
            ci = bootstrap_percentile_ci(samples, percentile=0.5)
            rows.append(
                {
                    "celula": cell_id,
                    "seletividade": tier,
                    "carga_rps": tier_entry.get("approx_throughput"),
                    "n_requisicoes": None,
                    "p50_ms": None,
                    "p95_ms": None,
                    "p99_ms": statistics.median(samples),
                    "ic95_p99_baixo_ms": ci.low,
                    "ic95_p99_alto_ms": ci.high,
                    "fonte_latencia": "resumo_por_repeticao_legado",
                    **_empty_resource_stats(),
                }
            )

    rows.sort(key=lambda r: (r["celula"], r["seletividade"], r["carga_rps"] or 0))
    return rows


def _write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=HEADER)
        writer.writeheader()
        writer.writerows(rows)


def export(results_root: Path, out_dir: Path, max_workers: int | None = None) -> list[dict]:
    rows = build_rows(results_root, max_workers=max_workers)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(rows, out_dir / "confirmacao_por_combo.csv")
    print(f"Tabela exportada em {out_dir} ({len(rows)} linhas).")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_root", type=Path)
    parser.add_argument("--out", type=Path, default=Path("docs/resultados_confirmacao"))
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="teto de processos em paralelo por combo (default: min(nº de combos, nº de CPUs))",
    )
    args = parser.parse_args(argv)
    export(args.results_root, args.out, max_workers=args.max_workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
