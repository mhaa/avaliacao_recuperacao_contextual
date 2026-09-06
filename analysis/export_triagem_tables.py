"""Exporta as tabelas da triagem em CSV para docs/resultados_triagem/ — uso
direto no texto do TCC (planilha/apêndice), fora do fluxo de
analysis/report.py (que produz report.json/pareto.png, não CSV). Reusa os
mesmos coletores de analysis/report.py (discover_rep_dirs, load_cell_*,
percentiles_of) para nunca haver uma segunda leitura/agregação divergente
dos mesmos dados brutos.

Gera 4 arquivos:
- triagem_resultados.csv / triagem_resultados_ptbr.csv (";" decimal ",",
  mesmo conteúdo): uma linha por célula, agregado das repetições.
- triagem_latencias_long.csv: uma linha por (célula, percentil) — formato
  longo, conveniente para gráficos comparativos.
- triagem_saturacao_sondagens.csv: uma linha por sondagem bruta da rampa de
  saturação (load/saturation.py) de cada célula.

Uso:
    python -m analysis.export_triagem_tables results --phase triagem \\
        --out docs/resultados_triagem
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from analysis.report import (
    discover_rep_dirs,
    ensure_collected,
    load_cell_latencies,
    load_cell_saturation,
    percentiles_of,
    storage_for_cell,
)

RESULTADOS_HEADER = [
    "cell_id",
    "estrategia",
    "banco",
    "run_timestamp",
    "n_repeticoes",
    "n_requisicoes_total",
    "taxa_erro_media",
    "vazao_atendida_media_rps",
    "latencia_p50_ms",
    "latencia_p95_ms",
    "latencia_p99_ms",
    "latencia_p999_ms",
    "vazao_saturacao_aprox_rps",
    "vazao_saturacao_censurada",
    "vazao_saturacao_limite_inferior_rps",
    "gerador_saturou_antes",
    "cpu_gerador_nao_medida_em_alguma_sondagem",
]


def estrategia_of(cell_id: str) -> str:
    return cell_id.split("-", 1)[0].upper()


def _rep_summaries(rep_dirs: list[Path]) -> dict[str, list[dict]]:
    """summary.json (analysis/collect.py) por rep, agrupado por célula —
    mesma extração de cell_id de load_cell_latencies (results/<cell_id>/
    <phase>/<timestamp>/rep<N>/)."""
    by_cell: dict[str, list[dict]] = {}
    for rep_dir in rep_dirs:
        cell_id = rep_dir.parents[2].name
        summary = json.loads((rep_dir / "summary.json").read_text())
        by_cell.setdefault(cell_id, []).append(summary)
    return by_cell


def _run_timestamp_of(rep_dirs: list[Path], cell_id: str) -> str:
    """Timestamp mais recente entre os rep_dirs da célula — mesmo critério
    de load_cell_saturation (nome do diretório, ordenável por ser
    UTC ISO-8601 compacto: AAAAMMDDThhmmssZ)."""
    timestamps = sorted(
        rep_dir.parent.name for rep_dir in rep_dirs if rep_dir.parents[2].name == cell_id
    )
    return timestamps[-1]


def build_resultados_rows(
    rep_dirs: list[Path],
    groups: dict[str, list[float]],
    saturation_by_cell: dict[str, dict],
) -> list[dict]:
    summaries_by_cell = _rep_summaries(rep_dirs)
    rows = []
    for cell_id, latencies in groups.items():
        summaries = summaries_by_cell[cell_id]
        percentiles = percentiles_of(latencies)
        saturation = saturation_by_cell.get(cell_id, {})
        rows.append(
            {
                "cell_id": cell_id,
                "estrategia": estrategia_of(cell_id),
                "banco": storage_for_cell(cell_id),
                "run_timestamp": _run_timestamp_of(rep_dirs, cell_id),
                "n_repeticoes": len(summaries),
                "n_requisicoes_total": sum(s["request_count"] for s in summaries),
                "taxa_erro_media": statistics.mean(s["error_rate"] for s in summaries),
                "vazao_atendida_media_rps": statistics.mean(
                    s["throughput_rps"] for s in summaries
                ),
                "latencia_p50_ms": percentiles["p50"],
                "latencia_p95_ms": percentiles["p95"],
                "latencia_p99_ms": percentiles["p99"],
                "latencia_p999_ms": percentiles["p999"],
                "vazao_saturacao_aprox_rps": saturation.get("approx_throughput"),
                "vazao_saturacao_censurada": saturation.get("censored", False),
                "vazao_saturacao_limite_inferior_rps": saturation.get("lower_bound"),
                "gerador_saturou_antes": saturation.get("loadgen_bottleneck", False),
                "cpu_gerador_nao_medida_em_alguma_sondagem": saturation.get(
                    "generator_cpu_unmeasured", False
                ),
            }
        )
    rows.sort(key=lambda r: r["cell_id"])
    return rows


def build_latencias_long_rows(groups: dict[str, list[float]]) -> list[dict]:
    rows = []
    for cell_id, latencies in groups.items():
        percentiles = percentiles_of(latencies)
        for percentil in ("p50", "p95", "p99", "p999"):
            rows.append(
                {
                    "cell_id": cell_id,
                    "estrategia": estrategia_of(cell_id),
                    "banco": storage_for_cell(cell_id),
                    "percentil": percentil,
                    "latencia_ms": percentiles[percentil],
                }
            )
    rows.sort(key=lambda r: (r["cell_id"], r["percentil"]))
    return rows


def build_saturacao_sondagens_rows(saturation_by_cell: dict[str, dict]) -> list[dict]:
    rows = []
    for cell_id, saturation in saturation_by_cell.items():
        for probe in saturation.get("probes", []):
            rows.append(
                {
                    "cell_id": cell_id,
                    "estrategia": estrategia_of(cell_id),
                    "banco": storage_for_cell(cell_id),
                    "vazao_sondada_rps": probe["rate"],
                    "p99_ms": probe["p99_ms"],
                    "violou_slo": probe["violated_slo"],
                    "cpu_gerador_pct": probe["generator_cpu_percent"],
                    "taxa_erro": probe["error_rate"],
                }
            )
    rows.sort(key=lambda r: (r["cell_id"], r["vazao_sondada_rps"]))
    return rows


def _write_csv(rows: list[dict], header: list[str], path: Path, decimal: str = ".") -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";" if decimal == "," else ",")
        writer.writerow(header)
        for row in rows:
            values = []
            for key in header:
                value = row[key]
                if decimal == "," and isinstance(value, float):
                    value = str(value).replace(".", ",")
                values.append(value)
            writer.writerow(values)


def export(results_root: Path, phase: str, out_dir: Path) -> None:
    rep_dirs = discover_rep_dirs(results_root, phase)
    if not rep_dirs:
        raise RuntimeError(f"Nenhum resultado encontrado em {results_root} para a fase {phase}.")
    ensure_collected(rep_dirs)
    saturation_by_cell = load_cell_saturation(rep_dirs)
    groups = load_cell_latencies(rep_dirs)

    out_dir.mkdir(parents=True, exist_ok=True)

    resultados_rows = build_resultados_rows(rep_dirs, groups, saturation_by_cell)
    _write_csv(resultados_rows, RESULTADOS_HEADER, out_dir / "triagem_resultados.csv")
    _write_csv(
        resultados_rows,
        RESULTADOS_HEADER,
        out_dir / "triagem_resultados_ptbr.csv",
        decimal=",",
    )

    latencias_rows = build_latencias_long_rows(groups)
    _write_csv(
        latencias_rows,
        ["cell_id", "estrategia", "banco", "percentil", "latencia_ms"],
        out_dir / "triagem_latencias_long.csv",
    )

    sondagens_rows = build_saturacao_sondagens_rows(saturation_by_cell)
    _write_csv(
        sondagens_rows,
        ["cell_id", "estrategia", "banco", "vazao_sondada_rps", "p99_ms", "violou_slo", "cpu_gerador_pct", "taxa_erro"],
        out_dir / "triagem_saturacao_sondagens.csv",
    )

    print(f"Tabelas exportadas em {out_dir} ({len(groups)} células).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_root", type=Path)
    parser.add_argument("--phase", required=True, choices=["triagem", "confirmacao"])
    parser.add_argument("--out", type=Path, default=Path("docs/resultados_triagem"))
    args = parser.parse_args(argv)
    export(args.results_root, args.phase, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
