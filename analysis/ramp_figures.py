"""Gera as três figuras da campanha de estresse a partir dos artefatos
baixados (docs/DESIGN.md, "Experimento complementar").

As três compartilham o MESMO eixo x — carga ofertada — de propósito: é o que
permite lê-las alinhadas, do tipo "na taxa em que a vazão descola da
diagonal, a CPU do banco está em X%".

Entradas (todas produzidas pela campanha, baixadas por
analysis/fetch_results.py):
  ramp_<tier>.json   degraus + veredito de recuperação (VM do gerador)
  resources.csv      3 VMs, 30 s, Cloud Monitoring (host)
  db_cpu_cores.csv   CPU do banco por núcleo, 5 s, /proc/stat (host)

As duas últimas são opcionais: uma execução abortada cedo pode não tê-las, e
faltar telemetria não deve impedir a geração das duas figuras de latência e
vazão, que só dependem do JSON.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from analysis.plots import (
    plot_offered_vs_sustained,
    plot_percentiles_by_step,
    plot_resources_by_step,
)
from analysis.ramp_resources import find_step, step_windows

# Curvas de CPU. A do banco aparece DUAS vezes, e é esse par que carrega o
# achado central nas células Valkey: agregado em ~12,5% contra máximo por
# núcleo em ~100% é a saturação de thread única que classify_bottleneck não
# consegue enxergar (analysis/ramp_resources.py).
DB_AGGREGATE_LABEL = "database (agregado)"
DB_MAX_CORE_LABEL = "database (máx. por núcleo)"


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _by_step_mean(per_step: dict, windows) -> list[tuple[float, float]]:
    """Série (taxa ofertada, média no degrau), na ordem das janelas — que é
    a ordem da trajetória da rampa, não a ordem das taxas."""
    points = []
    for _, _, key in windows:
        value = _mean(per_step.get(key, []))
        if value is not None:
            points.append((float(key[0]), value))
    return points


def resource_series(resources_csv: Path, windows) -> tuple[dict, dict]:
    """CPU e memória por componente, médias dentro de cada degrau."""
    cpu: dict[str, dict] = defaultdict(lambda: defaultdict(list))
    memory: dict[str, dict] = defaultdict(lambda: defaultdict(list))

    with resources_csv.open() as f:
        for row in csv.DictReader(f):
            if not row.get("timestamp"):
                continue
            key = find_step(windows, datetime.fromisoformat(row["timestamp"]))
            if key is None:
                continue
            component = row["component"]
            label = DB_AGGREGATE_LABEL if component == "database" else component
            cpu[label][key].append(float(row["cpu_percent"]))
            if row.get("memory_mb"):
                memory[component][key].append(float(row["memory_mb"]))

    cpu_points = {label: _by_step_mean(per_step, windows) for label, per_step in cpu.items()}
    mem_points = {label: _by_step_mean(per_step, windows) for label, per_step in memory.items()}
    return cpu_points, mem_points


def db_max_core_series(db_cpu_csv: Path, windows) -> list[tuple[float, float]]:
    """Por degrau, o PICO de um núcleo individual — não a média entre
    núcleos, que voltaria a esconder a thread única. A linha agregada `cpu`
    é excluída pelo mesmo motivo."""
    per_step: dict = defaultdict(list)
    with db_cpu_csv.open() as f:
        for row in csv.DictReader(f):
            if row["core"] == "cpu":
                continue
            key = find_step(windows, datetime.fromisoformat(row["timestamp"]))
            if key is not None:
                per_step[key].append(float(row["cpu_percent"]))

    points = []
    for _, _, key in windows:
        values = per_step.get(key)
        if values:
            points.append((float(key[0]), max(values)))
    return points


def generate(
    ramp_json: Path,
    out_dir: Path,
    resources_csv: Path | None = None,
    db_cpu_csv: Path | None = None,
) -> list[Path]:
    report = json.loads(ramp_json.read_text())
    steps = report["steps"]
    cell = report.get("cell_id", "")
    tier = report.get("tier", "")
    suffix = f"{cell} — seletividade {tier}" if cell else ""

    written = []
    offered = out_dir / f"ramp_offered_vs_sustained_{cell}.png"
    plot_offered_vs_sustained(steps, offered, title=f"Carga ofertada × vazão sustentada\n{suffix}")
    written.append(offered)

    percentiles = out_dir / f"ramp_percentiles_{cell}.png"
    plot_percentiles_by_step(steps, percentiles, title=f"Percentis por patamar\n{suffix}")
    written.append(percentiles)

    windows = step_windows(steps)
    cpu_points: dict = {}
    mem_points: dict = {}
    if resources_csv and resources_csv.exists():
        cpu_points, mem_points = resource_series(resources_csv, windows)
    if db_cpu_csv and db_cpu_csv.exists():
        cpu_points[DB_MAX_CORE_LABEL] = db_max_core_series(db_cpu_csv, windows)

    if cpu_points or mem_points:
        resources_png = out_dir / f"ramp_resources_{cell}.png"
        plot_resources_by_step(
            cpu_points, mem_points, resources_png, title=f"Recursos por patamar\n{suffix}"
        )
        written.append(resources_png)
    else:
        print("AVISO: sem resources.csv nem db_cpu_cores.csv — figura de recursos não gerada.")

    verdict = report.get("recovery", {}).get("verdict")
    degraded = sum(1 for s in steps if s.get("offered_load_ok") is False)
    print(
        f"{cell}/{tier}: {len(steps)} degraus, {degraded} com oferta deficitária, "
        f"recuperação={verdict}"
    )
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ramp_json", type=Path)
    parser.add_argument("--resources-csv", type=Path, default=None)
    parser.add_argument("--db-cpu-csv", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=Path("results/report/estresse"))
    args = parser.parse_args()

    # Default: irmãos do próprio ramp_<tier>.json, que é como o download os
    # deposita.
    resources_csv = args.resources_csv or args.ramp_json.parent / "resources.csv"
    db_cpu_csv = args.db_cpu_csv or args.ramp_json.parent / "db_cpu_cores.csv"

    for path in generate(args.ramp_json, args.out_dir, resources_csv, db_cpu_csv):
        print(f"figura: {path}")


if __name__ == "__main__":
    main()
