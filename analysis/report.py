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
from analysis.pareto import (
    SECONDS_PER_MONTH,
    TOLERANCE,
    capacity_units,
    cells_without_cost,
    censorship_warning,
    cheapest_cells,
    cost_per_million_requests,
    cost_per_million_requests_bounds,
    cost_undefined_reason,
    pareto_frontier,
)
from analysis.plots import (
    plot_pareto_frontier,
    plot_percentile_comparison,
)
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

# --------------------------------------------------------------------------
# Modelo de custo — ver docs/DESIGN.md, "Custo como função da demanda":
#
#     n   = ⌈V_mem / M⌉                    (1 para mecanismos em disco)
#     C_f = n · p_i · h
#     C_a = n · V_disco · p_a
#     C   = (C_f + C_a) · 10^6 / (S · 2.592.000)   [$ por milhão de requisições]
#
# A análise sempre opera na capacidade máxima de UMA unidade de atendimento —
# não há mais demanda `D` externa a varrer. A aritmética de custo/dominância
# vive em analysis/pareto.py; aqui só se montam as constantes e o custo POR
# UNIDADE (unit_cost_usd_month), que pareto.py normaliza pela vazão de
# saturação de cada célula.
# --------------------------------------------------------------------------

# Preços de COMPUTAÇÃO: preço de referência publicado para as regiões baseline
# dos EUA, aplicado SEM multiplicador regional. Decisão consciente: é um número
# publicado e verificável, ao contrário de uma estimativa obtida multiplicando
# a baseline por um prêmio regional que não foi conferido no console de
# faturamento. O custo do trabalho é, portanto, expresso em preço de
# referência dos EUA — não em preço específico de us-east4, que é cerca de 8%
# maior. Isso precisa estar declarado no texto do TCC.
# Consultado em 2026-09-06.
MACHINE_HOURLY_USD = {"n2-standard-4": 0.1942, "n2-standard-8": 0.3885}

HOURS_PER_MONTH = 730

# p_i — uma UNIDADE DE ATENDIMENTO é 1 VM de banco (n2-standard-8) + 1 VM de
# serviço (n2-standard-8). A VM geradora de carga NÃO entra: é aparato de
# medição, não capacidade produtiva. Sai igual nas 4 tecnologias porque todas
# usam os mesmos tipos de máquina — a discriminação de custo vem do
# denominador (S, a vazão de saturação) e da parcela de armazenamento por
# unidade.
SERVICE_UNIT_USD_HOUR = {
    storage: 2 * MACHINE_HOURLY_USD["n2-standard-8"]
    for storage in ("postgres", "valkey", "scylla", "opensearch")
}

# p_a — preço MENSAL por GiB de disco (pd-ssd, us-east4; fonte: tabela pública
# da GCP, consultada em 2026-09-06). Mensal e não horário de propósito: C_a
# precisa sair na mesma unidade de C_f = n·p_i·h, ou a soma das duas parcelas
# não significa nada.
DISK_USD_PER_GB_MONTH = 0.187

# M — memória útil por unidade de atendimento: o `--maxmemory 24gb` que
# infra/modules/database/main.tf configura no Valkey (a VM tem 32 GB).
#
# Memória NÃO tem preço próprio neste modelo, de propósito: a RAM já está paga
# dentro de p_i (o n2-standard-8 vem com 32 GB), e cobrá-la de novo por GiB
# seria dupla contagem. O que ela faz é LIMITAR quantas unidades cabem, via
# ⌈V_mem/M⌉ (analysis/pareto.py:capacity_units). É essa assimetria que
# representa a diferença real entre os dois meios: disco é elástico e
# faturado à parte; memória é limitada e já embutida na instância.
MEMORY_PER_UNIT_BYTES = 24 * 1024**3

# Carga fixa sob a qual a latência da triagem foi medida. Vai para o
# report.json porque o eixo de latência e o eixo de custo são observáveis
# independentes: sem registrar o ponto de operação da latência, o leitor não
# sabe a que regime o p99 se refere.
LATENCY_REFERENCE_LOAD_RPS = 1000.0

# GiB, não GB decimal: a GCP rotula "GB" nas tabelas de preço mas fatura em
# potências de 2 — a conversão aqui e o rótulo do preço têm de concordar.
_BYTES_PER_GIB = 1024**3

# O que cada estratégia realmente usa, por tecnologia — mapeado às
# tabelas/padrões de chave/índices dos adaptadores (storage/*.py), não ao
# modelo idealizado de dimensionamento.xlsx (que assume um deploy mínimo
# isolado por estratégia). Ver docs/DESIGN.md, "Custo de armazenamento",
# pela tabela completa e a justificativa de cada linha. Combinação ausente
# (ex.: ("e4", "scylla")) é célula inviável — nunca aparece em
# results/<cell>/, então unit_storage_cost_usd_month nunca é chamada para ela.
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


def storage_medium_for_cell(cell_id: str) -> str:
    """"memory" ou "disk" — decide por qual caminho o volume afeta o custo:
    disco entra em C_a (elástico, faturado por GiB); memória entra no piso de
    capacidade ⌈V_mem/M⌉ (limitada, já paga em p_i). Único lugar do projeto
    que testa por tecnologia residente em memória."""
    return "memory" if storage_for_cell(cell_id) == "valkey" else "disk"


def unit_storage_cost_usd_month(cell_id: str, storage_root: Path = _DEFAULT_STORAGE_ROOT) -> float:
    """C_a de UMA réplica, em $/mês.

    Zero para mecanismos em memória — e isso não quer dizer que o
    armazenamento deles seja de graça: quer dizer que ele é cobrado por outra
    via (o termo de capacidade de n(D)), não por GiB. Ver
    MEMORY_PER_UNIT_BYTES."""
    if storage_medium_for_cell(cell_id) == "memory":
        return 0.0
    gib = storage_bytes_for_cell(cell_id, storage_root) / _BYTES_PER_GIB
    return gib * DISK_USD_PER_GB_MONTH


def unit_compute_cost_usd_month(cell_id: str) -> float:
    """C_f de UMA unidade de atendimento, em $/mês (p_i · h)."""
    return SERVICE_UNIT_USD_HOUR[storage_for_cell(cell_id)] * HOURS_PER_MONTH


def unit_cost_usd_month(cell_id: str, storage_root: Path = _DEFAULT_STORAGE_ROOT) -> float:
    """Custo de UMA unidade de atendimento — o numerador que
    analysis/pareto.py normaliza pela vazão de saturação de cada célula."""
    return unit_compute_cost_usd_month(cell_id) + unit_storage_cost_usd_month(cell_id, storage_root)

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


def load_cell_saturation(results_root: Path, phase: str) -> dict[str, dict]:
    """Lê saturation.json (SaturationSearchResult de load/saturation.py) em
    results/<cell>/<phase>/<timestamp>/, tomando o timestamp mais recente por
    célula. Ausência para uma célula é normal — fica de fora do dict.

    Varre a árvore INDEPENDENTEMENTE dos rep_dirs, de propósito. A versão
    anterior derivava o diretório a partir de `rep_dir.parent`, o que só
    funciona quando latência e saturação foram gravadas na mesma execução.
    Uma execução só-rampa (`run_measurement_battery.py --only-saturation`,
    usada para re-medir S sem repetir a bateria de carga fixa) cria um
    diretório de timestamp novo contendo APENAS saturation.json, sem `rep*/`
    — e `discover_rep_dirs` não o enxerga. O relatório continuaria lendo o
    saturation.json ANTIGO sem emitir aviso nenhum, e todo o custo sairia
    calculado com o S velho."""
    saturation_by_cell: dict[str, dict] = {}
    newest_timestamp_by_cell: dict[str, str] = {}
    for path in results_root.glob(f"*/{phase}/*/saturation.json"):
        cell_id = path.parents[2].name
        timestamp = path.parent.name
        if timestamp > newest_timestamp_by_cell.get(cell_id, ""):
            newest_timestamp_by_cell[cell_id] = timestamp
            saturation_by_cell[cell_id] = json.loads(path.read_text())
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
        storage_bytes = storage_bytes_for_cell(cell_id, storage_root)
        medium = storage_medium_for_cell(cell_id)
        compute_month = unit_compute_cost_usd_month(cell_id)
        storage_month = unit_storage_cost_usd_month(cell_id, storage_root)
        cells.append(
            {
                "cell_id": cell_id,
                "latency_p99_ms": p99,
                "storage_bytes": storage_bytes,
                "storage_medium": medium,
                # memory_bytes só para tecnologias residentes em memória: é o
                # que alimenta ⌈V_mem/M⌉ em analysis/pareto.py. Para as de
                # disco fica 0, deixando o termo de capacidade inerte.
                "memory_bytes": storage_bytes if medium == "memory" else 0,
                "memory_per_unit_bytes": MEMORY_PER_UNIT_BYTES,
                "unit_compute_usd_month": compute_month,
                "unit_storage_usd_month": storage_month,
                "unit_cost_usd_month": compute_month + storage_month,
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

    cost_model_warnings = []

    for cell in cells:
        reason = cost_undefined_reason(cell)
        cell["cost_defined"] = reason is None
        cell["cost_undefined_reason"] = reason
        # Visível de propósito: hoje vale 1 em todas as células (o maior
        # V_mem é ~3,5 GiB contra M = 24 GiB), então o termo de capacidade
        # não está discriminando nada — declarar isso é melhor que deixar o
        # leitor supor que está.
        cell["capacity_bound_units"] = capacity_units(cell)
        # Estimativa PONTUAL — None para censuradas (só têm um teto, não um
        # ponto; ver cost_per_million_requests_usd_bounds).
        cell["cost_per_million_requests_usd"] = (
            cost_per_million_requests(cell) if reason is None else None
        )
        bounds = cost_per_million_requests_bounds(cell) if reason is None else None
        cell["cost_per_million_requests_usd_bounds"] = (
            {"low": bounds[0], "high": bounds[1]} if bounds is not None else None
        )

    without_cost = cells_without_cost(cells)
    for entry in without_cost:
        cost_model_warnings.append(
            f"{entry['cell_id']} fora do plano de custo — {entry['reason']}."
        )

    frontier = pareto_frontier(cells)
    tied_cheapest = cheapest_cells(cells)

    return {
        "cost_model": {
            "hours_per_month": HOURS_PER_MONTH,
            "seconds_per_month": SECONDS_PER_MONTH,
            "service_unit_usd_hour": SERVICE_UNIT_USD_HOUR["postgres"],
            "service_unit_usd_month": SERVICE_UNIT_USD_HOUR["postgres"] * HOURS_PER_MONTH,
            "disk_usd_per_gb_month": DISK_USD_PER_GB_MONTH,
            "memory_per_unit_bytes": MEMORY_PER_UNIT_BYTES,
            "tolerance": TOLERANCE,
            # Sem sharding: cada unidade guarda uma réplica integral, por isso
            # C_a escala com o piso de capacidade. É a premissa mais
            # contestável do modelo.
            "replication": "full_replica_per_unit",
            "byte_unit": "GiB (1024^3) — a GCP rotula 'GB' mas fatura GiB",
            "deployment_region": "us-east4",
            "price_sources": {
                # Regiões diferentes de propósito: usa-se, para cada item, o
                # número mais DIRETAMENTE publicado que existe. N2 não tem
                # tabela pública quebrada por região; pd-ssd tem.
                "compute": (
                    "preço de referência das regiões baseline dos EUA, sem multiplicador "
                    "regional; consultado em 2026-09-06. NÃO é o preço de us-east4, que é "
                    "cerca de 8% maior — declarar no texto"
                ),
                "disk": "tabela pública da GCP, pd-ssd us-east4; consultado em 2026-09-06",
            },
            "latency_reference_load_rps": LATENCY_REFERENCE_LOAD_RPS,
        },
        "kruskal_wallis": {
            "h_statistic": kruskal.h_statistic,
            "p_value": kruskal.p_value,
            "reject_h0": kruskal.reject_h0,
        },
        "dunn_posthoc": {f"{a}|{b}": p for (a, b), p in dunn.items()},
        "effect_size_epsilon_squared": epsilon_squared,
        "bootstrap_ci_p99": bootstrap_ci_p99,
        "cells": cells,
        "cells_without_cost": without_cost,
        "pareto_frontier": sorted(c["cell_id"] for c in frontier),
        "cheapest_cell_ids": tied_cheapest,
        "censorship_warning": censorship_warning(cells),
        "cost_model_warnings": cost_model_warnings,
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

    # docs/DESIGN.md, "Vazão ofertada verificada, não presumida": uma
    # repetição com offered_load_ok=False teve chegadas descartadas pelo k6
    # (maxVUs esgotado) — as latências dela são só das requisições
    # sobreviventes. Ela CONTINUA no relatório (o déficit em si é resultado:
    # a célula não sustenta o alvo), mas nunca sem este aviso.
    for rep_dir in rep_dirs:
        rep_summary = json.loads((rep_dir / "summary.json").read_text())
        if rep_summary.get("offered_load_ok") is False:
            print(
                f"AVISO: {rep_dir} — vazão ofertada em {rep_summary['offered_ratio']:.0%} do "
                f"alvo de {rep_summary['target_rate']} req/s (k6 descartou chegadas). As "
                "latências desta repetição subestimam a cauda sob o modelo aberto pretendido."
            )

    # results_root/phase, não rep_dirs: uma execução só-rampa grava um
    # timestamp novo sem rep*/ — ver load_cell_saturation.
    saturation_by_cell = load_cell_saturation(args.results_root, args.phase)
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
        report["cells"],
        args.out / "pareto.png",
        frontier_cell_ids=set(report["pareto_frontier"]),
        cost_by_cell={
            c["cell_id"]: c["cost_per_million_requests_usd"]
            for c in report["cells"]
            if c["cost_per_million_requests_usd"] is not None
        },
    )

    if report["censorship_warning"]:
        print(f"AVISO: {report['censorship_warning']}")
    for warning in report["cost_model_warnings"]:
        print(f"AVISO (custo): {warning}")

    if args.phase == "confirmacao":
        extra = build_confirmation_extras(groups)
        (args.out / "confirmacao_extra.json").write_text(json.dumps(extra, indent=2))
        percentiles_by_cell = {cell_id: percentiles_of(v) for cell_id, v in groups.items()}
        plot_percentile_comparison(percentiles_by_cell, args.out / "percentile_comparison.png")

    print(f"Relatório escrito em {args.out} ({len(groups)} células, {len(rep_dirs)} repetições).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
