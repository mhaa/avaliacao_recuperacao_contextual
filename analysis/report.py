"""Orquestra a análise em lote sobre `results/` — consolida as repetições de
várias células (via analysis/collect.py), roda a estatística exigida por
docs/DESIGN.md ("Estatística" / "Delineamento em duas etapas") e gera os
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
from analysis.pareto import censorship_warning, pareto_frontier
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
# o custo de COMPUTAÇÃO sai igual nas 4 — limitação conhecida do
# placeholder, não um bug. A componente de ARMAZENAMENTO abaixo é o que
# discrimina as tecnologias de verdade (ver docs/DESIGN.md, "Custo de
# armazenamento").
MACHINE_HOURLY_USD = {"n2-standard-4": 0.194, "n2-standard-8": 0.388}
CELL_COST_USD_HOUR = {
    storage: MACHINE_HOURLY_USD["n2-standard-8"] + MACHINE_HOURLY_USD["n2-standard-4"]
    for storage in ("postgres", "valkey", "scylla", "opensearch")
}

# Preço por GB — consultado direto na GCP em 2026-09-06 (não as estimativas
# de dimensionamento.xlsx — ver docs/DESIGN.md, "Custo de armazenamento",
# pela derivação completa e pela razão real memória:disco ≈16,5× que essa
# consulta encontrou, contra a estimativa a priori de ≈50× da planilha).
# Disco: pd-ssd, us-east4 (região do experimento) — $0,187/GB-mês (fonte:
# gcloud-compute.com, tabela pública da GCP).
DISK_USD_PER_GB_HOUR = 0.187 / 730
# Memória: N2 customizada não tem página de preço isolado — derivada de
# n2-standard-4 (4 vCPU, 16 GB, $0,1942/h) vs n2-highmem-4 (4 vCPU, 32 GB,
# $0,2620/h), ambos us-central1 (fonte: economize.cloud — melhor tabela
# pública encontrada; não achei preço de N2 customizada quebrado por região
# para us-east4 especificamente).
MEMORY_USD_PER_GB_HOUR = (0.2620 - 0.1942) / 16

_BYTES_PER_GB = 1024**3

# O que cada estratégia realmente usa, por tecnologia — mapeado às
# tabelas/padrões de chave/índices dos adaptadores (storage/*.py), não ao
# modelo idealizado de dimensionamento.xlsx (que assume um deploy mínimo
# isolado por estratégia). Ver docs/DESIGN.md, "Custo de armazenamento",
# pela tabela completa e a justificativa de cada linha. Combinação ausente
# (ex.: ("e4", "scylla")) é célula inviável — nunca aparece em
# results/<cell>/, então storage_cost_usd_hour nunca é chamada para ela.
STRATEGY_STORAGE_KEYS: dict[tuple[str, str], list[str]] = {
    ("e1", "postgres"): ["candidates"],
    ("e2", "postgres"): ["candidates", "item_contexts"],
    ("e3", "postgres"): ["prematerialized"],
    ("e4", "postgres"): ["candidates", "inverted_lists"],
    ("e1", "valkey"): ["candidates:*"],
    ("e2", "valkey"): ["candidates:*", "item_contexts:*"],
    ("e3", "valkey"): ["prematerialized:*"],
    ("e4", "valkey"): ["candidates_set:*", "inverted:*"],
    ("e1", "scylla"): ["candidates"],
    ("e2", "scylla"): ["candidates_by_context"],
    ("e3", "scylla"): ["prematerialized"],
    ("e1", "opensearch"): ["candidates"],
    ("e2", "opensearch"): ["candidates"],
    ("e4", "opensearch"): ["candidates"],
}


_DEFAULT_STORAGE_ROOT = Path("results/storage")


def _load_storage_sizes(storage: str, storage_root: Path) -> dict:
    """Lê <storage_root>/<storage>.json — escrito por
    infra/scripts/measure_storage_size.py, uma vez por tecnologia (nunca
    por célula: armazenamento não varia com carga/taxa de requisição).
    Ausência é erro, não 0 silencioso: misturar células com e sem custo de
    armazenamento no mesmo relatório enviesaria a fronteira de Pareto sem
    aviso nenhum. `storage_root` é parâmetro (não uma constante fixa) para
    os testes poderem apontar pra uma árvore sintética em `tmp_path`, sem
    tocar `results/` de verdade nem precisar de cache entre chamadas."""
    path = storage_root / f"{storage}.json"
    if not path.exists():
        raise RuntimeError(
            f"{path} não existe — rode infra/scripts/measure_storage_size.py {storage} "
            "... antes de gerar o relatório (docs/DESIGN.md, 'Custo de armazenamento'). "
            "Sem isso, custo_usd_hora ficaria incompleto (só computação) para as células dessa "
            "tecnologia."
        )
    return json.loads(path.read_text())["sizes"]


def storage_bytes_for_cell(cell_id: str, storage_root: Path = _DEFAULT_STORAGE_ROOT) -> int:
    strategy, storage = cell_id.split("-", 1)
    sizes = _load_storage_sizes(storage, storage_root)
    keys = STRATEGY_STORAGE_KEYS[(strategy, storage)]
    if storage == "valkey":
        # valkey_key_pattern_bytes (analysis/storage_size.py) devolve uma
        # estimativa por amostragem, não uma contagem exata — único dos 4
        # storages com essa característica (ver docs/DESIGN.md).
        return sum(sizes[key]["bytes_estimate"] for key in keys)
    return sum(sizes[key] for key in keys)


def storage_cost_usd_hour(cell_id: str, storage_root: Path = _DEFAULT_STORAGE_ROOT) -> float:
    storage = cell_id.split("-", 1)[1]
    price_per_gb_hour = MEMORY_USD_PER_GB_HOUR if storage == "valkey" else DISK_USD_PER_GB_HOUR
    return (storage_bytes_for_cell(cell_id, storage_root) / _BYTES_PER_GB) * price_per_gb_hour

# Margem de equivalência prática para o TOST na confirmação — placeholder,
# confirmar com o usuário o valor real antes de usar num resultado do TCC.
EQUIVALENCE_MARGIN_MS = 10.0


def discover_rep_dirs(results_root: Path, phase: str) -> list[Path]:
    # requests.ndjson, não k6-raw.json: é o arquivo que analysis/collect.py
    # de fato lê agora (console.log de load/scenarios.js, uma linha por
    # requisição) — k6-raw.json (saída nativa --out json= do k6) continua
    # sendo gravado, mas nada mais o lê.
    return sorted(
        p.parent
        for p in results_root.glob(f"*/{phase}/*/rep*/requests.ndjson")
    )


def ensure_collected(rep_dirs: list[Path]) -> None:
    for rep_dir in rep_dirs:
        if not (rep_dir / "summary.json").exists():
            collect(rep_dir)


def load_cell_saturation(rep_dirs: list[Path]) -> dict[str, dict]:
    """Lê saturation.json (um por execução de
    infra/scripts/run_measurement_battery.py, em
    results/<cell>/<phase>/<timestamp>/saturation.json — SaturationSearchResult
    de load/saturation.py). Se uma célula tiver mais de uma execução, usa a
    do timestamp mais recente. Ausência de saturation.json para uma célula
    é normal (rodadas anteriores a esta etapa) — fica de fora do dict."""
    timestamp_dir_by_cell: dict[str, Path] = {}
    for rep_dir in rep_dirs:
        cell_id = rep_dir.parents[2].name
        timestamp_dir = rep_dir.parent
        current = timestamp_dir_by_cell.get(cell_id)
        if current is None or timestamp_dir.name > current.name:
            timestamp_dir_by_cell[cell_id] = timestamp_dir

    saturation_by_cell: dict[str, dict] = {}
    for cell_id, timestamp_dir in timestamp_dir_by_cell.items():
        saturation_path = timestamp_dir / "saturation.json"
        if saturation_path.exists():
            saturation_by_cell[cell_id] = json.loads(saturation_path.read_text())
    return saturation_by_cell


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


def build_report(
    groups: dict[str, list[float]],
    saturation_by_cell: dict[str, dict] | None = None,
    storage_root: Path = _DEFAULT_STORAGE_ROOT,
) -> dict:
    saturation_by_cell = saturation_by_cell or {}
    labels = list(groups)
    kruskal = kruskal_wallis([groups[label] for label in labels])
    dunn = dunn_posthoc(groups) if kruskal.reject_h0 else {}
    n_total = sum(len(v) for v in groups.values())
    epsilon_squared = effect_size_epsilon_squared(kruskal.h_statistic, n_total, len(labels))

    cells = []
    bootstrap_ci_p99 = {}
    for cell_id, latencies in groups.items():
        p99 = percentiles_of(latencies)["p99"]
        saturation = saturation_by_cell.get(cell_id, {})
        compute_cost = CELL_COST_USD_HOUR[storage_for_cell(cell_id)]
        storage_bytes = storage_bytes_for_cell(cell_id, storage_root)
        storage_cost = storage_cost_usd_hour(cell_id, storage_root)
        cells.append(
            {
                "cell_id": cell_id,
                "latency_p99_ms": p99,
                "cost_usd_hour": compute_cost + storage_cost,
                "compute_cost_usd_hour": compute_cost,
                "storage_cost_usd_hour": storage_cost,
                "storage_bytes": storage_bytes,
                "saturation_throughput_approx": saturation.get("approx_throughput"),
                "saturation_censored": saturation.get("censored", False),
                "saturation_lower_bound": saturation.get("lower_bound"),
                # .get com default: saturation.json arquivado ANTES desta
                # chave existir continua legível (naqueles, 0.0 é ambíguo
                # entre "ocioso" e "não medido" — ver README, Fase 5).
                "saturation_generator_cpu_unmeasured": saturation.get(
                    "generator_cpu_unmeasured", False
                ),
            }
        )
        ci = bootstrap_percentile_ci(latencies, percentile=0.99)
        bootstrap_ci_p99[cell_id] = {"low": ci.low, "high": ci.high}

    frontier = pareto_frontier(cells)

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
        "pareto_frontier": [c["cell_id"] for c in frontier],
        "censorship_warning": censorship_warning(cells),
    }


def build_confirmation_extras(groups: dict[str, list[float]]) -> dict:
    """TOST par-a-par entre as células da fronteira — 'não rejeitou H0' não
    é o mesmo que 'equivalente na prática' (docs/DESIGN.md, "Delineamento em
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
    saturation_by_cell = load_cell_saturation(rep_dirs)
    groups = load_cell_latencies(rep_dirs)

    # loadgen_bottleneck invalida só a VAZÃO medida (o gerador saturou antes
    # da célula) — nunca a latência/custo já coletados sob carga fixa, que
    # continuam válidos. Descartar a célula inteira jogaria fora dado bom.
    bottlenecked_cells = {
        cell_id for cell_id, s in saturation_by_cell.items() if s.get("loadgen_bottleneck")
    }
    for cell_id in bottlenecked_cells:
        print(
            f"AVISO: {cell_id} — o gerador de carga saturou durante a rampa (loadgen_bottleneck); "
            "a vazão de saturação desta célula fica sem dado (nem censurada, nem um valor) até o "
            "gerador ser escalado e a rampa repetida. Latência/custo continuam válidos."
        )
        saturation_by_cell.pop(cell_id, None)

    # generator_cpu_unmeasured NÃO é gargalo confirmado — é o portão de
    # validade (CPU do gerador < 60%) que ficou SEM avaliação naquela rampa.
    # Diferente de loadgen_bottleneck, a vazão medida continua no relatório:
    # descartá-la jogaria fora dado provavelmente bom por causa de uma falha
    # de telemetria. Mas vai avisada aqui e marcada célula a célula em
    # report.json, para nunca ser lida como validada.
    for cell_id, s in saturation_by_cell.items():
        if s.get("generator_cpu_unmeasured"):
            print(
                f"AVISO: {cell_id} — ao menos uma sondagem da rampa ficou sem leitura de CPU do "
                "gerador; a vazão está no relatório, mas o portão dos 60% não foi avaliado nela. "
                "Trate como não validada nessa dimensão."
            )

    report = build_report(groups, saturation_by_cell)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "report.json").write_text(json.dumps(report, indent=2))
    plot_pareto_frontier(
        report["cells"], args.out / "pareto.png", frontier_cell_ids=set(report["pareto_frontier"])
    )
    if report["censorship_warning"]:
        print(f"AVISO: {report['censorship_warning']}")

    if args.phase == "confirmacao":
        extra = build_confirmation_extras(groups)
        (args.out / "confirmacao_extra.json").write_text(json.dumps(extra, indent=2))
        percentiles_by_cell = {cell_id: percentiles_of(v) for cell_id, v in groups.items()}
        plot_percentile_comparison(percentiles_by_cell, args.out / "percentile_comparison.png")

    print(f"Relatório escrito em {args.out} ({len(groups)} células, {len(rep_dirs)} repetições).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
