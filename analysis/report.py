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
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
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
    vargha_delaney_a,
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
    # requisição) — k6-raw.json não fica mais no disco (load/upload_one_file.py
    # sobe e apaga cada um logo após a repetição).
    # "**" (não "*/*"): triagem grava rep<N>/ direto sob <timestamp>/, mas
    # confirmação grava sob <timestamp>/<rate>-<tier>/rep<N>/ (um nível a mais
    # — load/run_battery.py:combo_out_dir, docs/ARCHITECTURE.md) para as 9
    # combinações não caírem nos mesmos 5 diretórios. "**" casa as duas
    # profundidades sem este código precisar saber qual é qual.
    all_rep_dirs = sorted(
        p.parent
        for p in results_root.glob(f"*/{phase}/**/rep*/requests.ndjson")
    )
    # Só o timestamp mais recente por célula — mesma disciplina de
    # load_cell_saturation (ver docstring lá). Sem isto, tentativas antigas
    # (retries, execuções interrompidas, reexecuções pós-fix de arquitetura)
    # ficam acumuladas em results/<cell>/<phase>/<timestamp>/ e suas
    # latências se misturam silenciosamente com a execução válida mais
    # recente — confirmado ao vivo: as 4 células da campanha de confirmação
    # têm de 2 a 4 timestamps cada uma no bucket de resultados.
    cell_and_timestamp = {
        rep_dir: (rep_dir.relative_to(results_root).parts[0], rep_dir.relative_to(results_root).parts[2])
        for rep_dir in all_rep_dirs
    }
    newest_timestamp_by_cell: dict[str, str] = {}
    for cell_id, timestamp in cell_and_timestamp.values():
        if timestamp > newest_timestamp_by_cell.get(cell_id, ""):
            newest_timestamp_by_cell[cell_id] = timestamp
    return [
        rep_dir
        for rep_dir, (cell_id, timestamp) in cell_and_timestamp.items()
        if timestamp == newest_timestamp_by_cell[cell_id]
    ]


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
    calculado com o S velho.

    Confirmação grava saturation_<tier>.json (um por seletividade — rampa
    adaptativa por seletividade, docs/DESIGN.md "Rampa de confirmação",
    commit efcc1ff) em vez de um saturation.json único. Sem tratar isso à
    parte, este dict voltava vazio para as 4 células de confirmação e o
    custo/Pareto do relatório final saía ausente pra elas, silenciosamente.
    Os 3 valores ficam preservados em by_tier (nada se perde); a
    seletividade "high" (a menos restritiva — mais perto do acesso
    irrestrito ao catálogo, portanto a leitura mais próxima do teto de
    capacidade da célula) também é exposta no nível de topo do dict, como
    representante único para o cálculo de custo em build_report — escolha
    documentada aqui, não escondida atrás de uma média ou heurística
    opaca."""
    saturation_by_cell: dict[str, dict] = {}
    newest_timestamp_by_cell: dict[str, str] = {}
    for path in results_root.glob(f"*/{phase}/*/saturation.json"):
        cell_id = path.parents[2].name
        timestamp = path.parent.name
        if timestamp > newest_timestamp_by_cell.get(cell_id, ""):
            newest_timestamp_by_cell[cell_id] = timestamp
            saturation_by_cell[cell_id] = json.loads(path.read_text())

    by_tier_per_cell_and_timestamp: dict[tuple[str, str], dict[str, dict]] = {}
    for path in results_root.glob(f"*/{phase}/*/saturation_*.json"):
        cell_id = path.parents[2].name
        timestamp = path.parent.name
        tier = path.stem.removeprefix("saturation_")
        by_tier_per_cell_and_timestamp.setdefault((cell_id, timestamp), {})[tier] = json.loads(
            path.read_text()
        )
    newest_tiered_timestamp_by_cell: dict[str, str] = {}
    for (cell_id, timestamp), by_tier in by_tier_per_cell_and_timestamp.items():
        if timestamp > newest_tiered_timestamp_by_cell.get(cell_id, ""):
            newest_tiered_timestamp_by_cell[cell_id] = timestamp
            entry = dict(by_tier.get("high", {}))
            entry["by_tier"] = by_tier
            saturation_by_cell[cell_id] = entry

    return saturation_by_cell


def load_cell_latencies(rep_dirs: list[Path]) -> dict[str, list[float]]:
    """Agrupa por cell_id, concatenando latency_ms de todas as repetições
    daquela célula — na triagem, de results/<cell_id>/<phase>/<timestamp>/
    rep<N>/; na confirmação, de .../<timestamp>/<rate>-<tier>/rep<N>/ (um
    nível a mais, load/run_battery.py:combo_out_dir). cell_id vem de
    manifest.json, não da profundidade do caminho — as duas layouts
    convivem sem este código precisar saber qual é qual."""
    by_cell: dict[str, list[float]] = {}
    for rep_dir in rep_dirs:
        cell_id = json.loads((rep_dir / "manifest.json").read_text())["cell_id"]
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


def load_cell_returned_counts(rep_dirs: list[Path]) -> dict[str, list[tuple[int, int]]]:
    """Agrupa (returned_count, k) por "<cell_id>|<selectivity_tier>", um par
    por requisição — mesma disciplina de load_cell_latencies (cell_id do
    manifest.json, não da profundidade do caminho); k e selectivity_tier
    vêm do mesmo manifest.json de cada repetição, nunca presumidos
    constantes entre repetições.

    returned_count já está em latencies.parquet (analysis/collect.py escreve
    a coluna; core/contract.py: "a contagem real de itens elegíveis quando
    menor que k" — a resposta pode legitimamente vir parcial quando poucos
    candidatos sobrevivem ao predicado). Pareado com k aqui para
    returned_count_stats poder calcular a fração de respostas parciais."""
    by_key: dict[str, list[tuple[int, int]]] = {}
    for rep_dir in rep_dirs:
        manifest = json.loads((rep_dir / "manifest.json").read_text())
        key = f"{manifest['cell_id']}|{manifest['selectivity_tier']}"
        k = manifest["k"]
        df = pl.read_parquet(rep_dir / "latencies.parquet")
        by_key.setdefault(key, []).extend((rc, k) for rc in df["returned_count"].to_list())
    return by_key


def returned_count_stats(pairs: list[tuple[int, int]]) -> dict:
    """Média de itens por resposta e fração de respostas parciais
    (returned_count < k) para um grupo de (returned_count, k) — tipicamente
    uma célula num patamar de seletividade (load_cell_returned_counts).
    `n=0` (grupo vazio) devolve Nones em vez de dividir por zero."""
    if not pairs:
        return {"mean_returned_count": None, "partial_response_rate": None, "n": 0}
    counts = [rc for rc, _ in pairs]
    partial = sum(1 for rc, k in pairs if rc < k)
    return {
        "mean_returned_count": sum(counts) / len(counts),
        "partial_response_rate": partial / len(pairs),
        "n": len(pairs),
    }


def _compute_cell_report_entry(
    cell_id: str,
    latencies: np.ndarray,
    saturation: dict,
    storage_root: Path,
) -> tuple[dict, dict]:
    """Trabalho pesado de UMA célula (percentil + IC bootstrap do p99) —
    função de módulo (não um closure) de propósito: ProcessPoolExecutor
    precisa dar pickle na função e nos argumentos para mandar a outro
    processo. Roda num processo separado por célula (build_report) porque
    bootstrap_percentile_ci é single-threaded (np.percentile/sort não
    paraleliza via BLAS) — confirmado ao vivo numa VM de 8 núcleos rodando
    só 1 (load average 1.00, ~103% de CPU), enquanto a confirmação pool
    20-30M+ linhas por célula em vez das ~1.5M da triagem, elevando o tempo
    de minutos para horas num único núcleo."""
    started = time.monotonic()
    n = len(latencies)
    print(f"[{cell_id}] iniciando (N={n} requisições)...", flush=True)
    p99 = percentiles_of(latencies)["p99"]
    storage_bytes = storage_bytes_for_cell(cell_id, storage_root)
    medium = storage_medium_for_cell(cell_id)
    compute_month = unit_compute_cost_usd_month(cell_id)
    storage_month = unit_storage_cost_usd_month(cell_id, storage_root)
    ci = bootstrap_percentile_ci(latencies, percentile=0.99)
    elapsed = time.monotonic() - started
    print(f"[{cell_id}] concluído em {elapsed:.1f}s (p99={p99:.2f}ms)", flush=True)
    entry = {
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
        "saturation_generator_cpu_unmeasured": saturation.get("generator_cpu_unmeasured", False),
    }
    return entry, {"low": ci.low, "high": ci.high}


def build_report(
    groups: dict[str, list[float]],
    saturation_by_cell: dict[str, dict] | None = None,
    storage_root: Path = _DEFAULT_STORAGE_ROOT,
    returned_counts_by_key: dict[str, list[tuple[int, int]]] | None = None,
) -> dict:
    saturation_by_cell = saturation_by_cell or {}
    returned_counts_by_key = returned_counts_by_key or {}
    labels = list(groups)
    kruskal = kruskal_wallis([groups[label] for label in labels])
    dunn = dunn_posthoc(groups) if kruskal.reject_h0 else {}
    n_total = sum(len(v) for v in groups.values())
    epsilon_squared = effect_size_epsilon_squared(kruskal.h_statistic, n_total, len(labels))
    # A de Vargha-Delaney por par — ao contrário de dunn_posthoc, não fica
    # atrás do gate reject_h0: não é um teste de hipótese (sem taxa de
    # falso-positivo a proteger), é a magnitude por par que falta quando
    # Dunn sozinho satura em "diferente" pra todo par (amostras de centenas
    # de milhares de requisições por célula — ver analysis/stats.py:
    # vargha_delaney_a). Roda pra todos os pares independente do resultado
    # do Kruskal-Wallis.
    vargha_delaney = {
        (a, b): vargha_delaney_a(groups[a], groups[b])
        for a in labels
        for b in labels
        if a != b
    }

    # Um processo por célula: percentiles_of + bootstrap_percentile_ci são o
    # gargalo real (bootstrap é single-threaded, np.percentile/sort não
    # paraleliza via BLAS — confirmado ao vivo numa VM de 8 núcleos rodando
    # só 1). max_workers limitado a os.cpu_count() (nunca mais que o nº de
    # células, ProcessPoolExecutor não reaproveita workers ociosos além do
    # necessário) — cada worker aloca seu próprio lote do bootstrap
    # (analysis/stats.py:_BOOTSTRAP_BATCH_TARGET_BYTES), então o teto de
    # memória agregado escala com max_workers, não com o nº de células.
    max_workers = max(1, min(len(labels), os.cpu_count() or 1))
    print(
        f"Calculando estatísticas por célula ({len(labels)} células, até "
        f"{max_workers} em paralelo)...",
        flush=True,
    )
    results_by_cell: dict[str, tuple[dict, dict]] = {}
    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(
                _compute_cell_report_entry,
                cell_id,
                np.asarray(latencies, dtype=float),
                saturation_by_cell.get(cell_id, {}),
                storage_root,
            ): cell_id
            for cell_id, latencies in groups.items()
        }
        for future in as_completed(futures):
            cell_id = futures[future]
            results_by_cell[cell_id] = future.result()

    # Ordem determinística (sorted por cell_id), não a ordem de conclusão do
    # ProcessPoolExecutor — mesma disciplina de discover_rep_dirs.
    cells = [results_by_cell[cell_id][0] for cell_id in sorted(results_by_cell)]
    bootstrap_ci_p99 = {cell_id: ci for cell_id, (_, ci) in results_by_cell.items()}

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
        "vargha_delaney_a": {f"{a}|{b}": v for (a, b), v in vargha_delaney.items()},
        "effect_size_epsilon_squared": epsilon_squared,
        "bootstrap_ci_p99": bootstrap_ci_p99,
        # Média de itens por resposta e fração de respostas parciais
        # (returned_count < k), por "<cell_id>|<selectivity_tier>" — na
        # triagem colapsa num só patamar por célula; na confirmação, um por
        # seletividade testada (load_cell_returned_counts).
        "returned_count_by_cell_tier": {
            key: returned_count_stats(pairs) for key, pairs in returned_counts_by_key.items()
        },
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
    duas etapas"). `vargha_delaney_a` acompanha cada par: mesmo quando o TOST
    aponta "não equivalente" pra uma margem escolhida, A12 dá a magnitude e a
    direção da diferença (0,5 = sem diferença; ~0,71 = grande, Vargha &
    Delaney 2000), útil pra saber SE vale a pena apertar a margem ou se a
    diferença é grande demais pra ser só um efeito de amostra."""
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
                "vargha_delaney_a": vargha_delaney_a(groups[a], groups[b]),
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
    returned_counts_by_key = load_cell_returned_counts(rep_dirs)

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

    report = build_report(
        groups, saturation_by_cell, returned_counts_by_key=returned_counts_by_key
    )
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
