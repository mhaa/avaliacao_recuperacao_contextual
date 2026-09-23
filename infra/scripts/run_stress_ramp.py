"""Campanha complementar de estresse com foco no banco (docs/DESIGN.md,
"Experimento complementar — estresse com foco no banco").

Script IRMÃO de run_measurement_battery.py, não uma fase dentro dele. O plano
previa uma fase nova lá; separar mostrou-se estritamente melhor para as duas
restrições impostas:

- **Não perturbar o que já existe.** `run_measurement_battery.py` não é
  tocado em nenhuma linha, então seus ~57 testes continuam válidos sem
  reinterpretação, e o caminho que produziu os dados da campanha principal
  não muda de comportamento.
- **Isolamento estrutural.** `build_sweep`, `_high_rate_from_saturation` e a
  bateria de carga fixa não têm significado nesta campanha; enfiar um quarto
  valor de `--phase` por dentro deles criaria ramos mortos em código que
  decide gasto real de nuvem.

O que é REUSADO (import, nunca cópia): provisionamento, SSH com retry, setup
de dataset, prontidão do serviço, amostragem de recursos e o padrão de
comando remoto — todos de run_measurement_battery.py e cloud_smoke_test.py.

Isolamento em quatro camadas, detalhado em infra/envs/estresse/main.tf:
root module próprio, prefixo de estado `estresse/<cell>`, TF_DATA_DIR
`.terraform-estresse-<cell>` e `name_suffix="-st"` nos nomes de recurso.

NADA aqui é apagado. Diferente do caminho principal (que faz `shutil.rmtree`
de `results/<cell>` no início de cada execução), esta campanha recusa começar
se o destino já existir.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import shlex
import sys
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from analysis.ramp_resources import max_core_percent, samples_from_log, write_db_cpu_cores_csv
from analysis.resources import ResourceSample, classify_bottleneck, write_resources_csv
from infra.scripts.cloud_smoke_test import (
    _confirm_billable,
    _run,
    fetch_terraform_access_token,
    gcloud_ssh,
    gcloud_ssh_with_retry,
    restart_container,
    storage_for_cell,
    terraform,
    terraform_output_json,
    wait_for_container,
    wait_for_service_ready,
)
from infra.scripts.run_measurement_battery import (
    DEFAULT_MEMORY_MB_BY_COMPONENT,
    FIXTURES_MOUNT,
    MAIN_MEASUREMENT_USER_COUNT,
    RESULTS_MOUNT,
    _parse_probe_result,
    build_remote_probe_aggregate_command,
    build_remote_probe_rep_command,
    build_remote_setup_command,
    build_remote_upload_command,
    make_resource_collect_fn,
    sample_resources_periodically,
    snapshot_exists,
)
from load.ramp import build_step_schedule, check_disk_budget
from load.run_battery import build_ramp_k6_cmd
from load.saturation import CEILING_RPS, ProbeResult, run_linear_probe_sweep, run_saturation_search

# Diretório do root module próprio. `terraform()` já aceita `tf_dir`, e
# `cell=` já define TF_DATA_DIR — as duas camadas de isolamento saem de
# graça, sem tocar em cloud_smoke_test.py.
TF_DIR = "infra/envs/estresse"

# Prefixo de resultados, fora do namespace que analysis/report.py varre.
# Ver analysis/fetch_results.py:ESTRESSE_PREFIX — mesma constante, mesma
# razão.
ESTRESSE_PREFIX = "_estresse"

# Só a seletividade média: a campanha caracteriza o banco, não refaz a
# matriz. É a mesma que a triagem usa como referência.
TIER = "medium"

# Sondagem rápida do joelho, ANTES de montar a rampa. Existe porque o
# cronograma da rampa é estático: a fase fina cobre [0,6·K, 1,3·K] em torno do
# joelho, então um K projetado errado em mais de ~40% para baixo joga a
# violação na fase grossa — resolução 10x pior — e gasta a fase fina inteira
# em sobrecarga, devolvendo "recovery=undetermined" depois de horas de VM
# pagas. Medir K custa ~20 min contra as 2-3,4 h da rampa.
#
# 30s de aquecimento é TRÁFEGO REAL na mesma taxa (load/scenarios.js), não
# ociosidade: sem ele a medição começa a frio, com conexões novas e pools
# vazios, e a leitura de /proc/stat que cerca a sondagem dilui a CPU do
# gerador com tempo parado — afrouxando o portão dos 60%.
PROBE_WARMUP = "30s"
# 60s basta para o percentil estabilizar nas taxas em questão (a 10k req/s
# são 600 mil requisições) — a precisão fina não vem daqui, vem da rampa
# construída em torno do valor achado.
PROBE_MEASURE = "60s"
# Patamares em dobra (1000, 2000, 4000...) enquadram o joelho em no máximo 7
# sondagens; a busca binária refina depois. É o caminho mais curto até um
# bracket, e `doubling_sequence` já existe e é testado em load/saturation.py.
PROBE_STEP_MODE = "doubling"
PROBE_START_RATE = 1_000

# --quick-probe (varredura linear, load/saturation.py:run_linear_probe_sweep).
# 5s de aquecimento, não 0s: um aquecimento zerado dilui a leitura de CPU do
# gerador que _generator_saturated usa (mesmo motivo do PROBE_WARMUP de 30s
# acima) — a sondagem rápida é exploratória, não autoritativa, mas o portão
# dos 60% que ela protege é exatamente o que garante que "o banco satura
# primeiro" não seja, na verdade, "o gerador satura primeiro" — enfraquecê-lo
# por alguns segundos de economia contradiria o propósito da própria sondagem.
QUICK_PROBE_WARMUP = "5s"
# 30s por patamar: mesmo grão da fase grossa da rampa completa
# (load/ramp.py:DEFAULT_COARSE_DURATION_S) — a sondagem rápida existe para
# mapear a curva com essa resolução, não com a precisão de PROBE_MEASURE.
QUICK_PROBE_MEASURE = "30s"

# Amostragem de /proc/stat por núcleo na VM do BANCO. 5s dá ~12 pontos por
# degrau fino de 60s, contra ~1 ponto borrado do Cloud Monitoring — ver
# analysis/ramp_resources.py para as duas razões (resolução e a cegueira do
# agregado ao Valkey de thread única).
PROC_STAT_INTERVAL_S = 5
PROC_STAT_REMOTE_PATH = "/tmp/proc_stat.log"

# Margem sobre a duração do cronograma para o amostrador cobrir a rampa
# inteira mesmo com atraso de partida do k6.
PROC_STAT_MARGIN_S = 120

# Dimensionamento por célula (docs/DESIGN.md e infra/envs/estresse/main.tf).
# Joelho projetado a partir de results/report/extra/
# resource_bottleneck_by_cell_tier.csv, tier medium, até o teto arquitetural
# de cada tecnologia.
CELL_DEFAULTS = {
    "e4-valkey": {
        "knee": 4_900,
        "fine_step": 100,
        "service": "n2-custom-16-32768",
        "loadgen": "n2-highcpu-16",
    },
    "e2-scylla": {
        "knee": 11_300,
        "fine_step": 100,
        "service": "n2-highcpu-32",
        "loadgen": "n2-highcpu-16",
    },
    "e3-postgres": {
        "knee": 30_400,
        "fine_step": 300,
        "service": "n2-highcpu-64",
        "loadgen": "n2-highcpu-64",
    },
    "e3-valkey": {
        "knee": 38_300,
        "fine_step": 400,
        "service": "n2-highcpu-96",
        "loadgen": "n2-highcpu-96",
    },
}

# Memória nominal por tipo de máquina, para converter a fração que o coletor
# OTel reporta em MB. Espelha DEFAULT_MEMORY_MB_BY_COMPONENT do caminho
# principal, que assume n2-standard-8 em tudo — aqui serviço e gerador
# mudam, então o valor tem de acompanhar ou a classificação de gargalo sai
# errada.
MACHINE_MEMORY_MB = {
    "n2-standard-8": 32768.0,
    "n2-custom-16-32768": 32768.0,
    "n2-highcpu-16": 16384.0,
    "n2-highcpu-32": 32768.0,
    "n2-highcpu-64": 65536.0,
    "n2-highcpu-96": 98304.0,
}


def make_stress_probe_fn(
    cell_id: str,
    target_url: str,
    tier: str,
    loadgen_instance: str,
    zone: str,
    project_id: str,
    tools_image: str,
    results_bucket: str,
    run_timestamp: str,
    *,
    user_count: int,
    warmup: str = PROBE_WARMUP,
    measure: str = PROBE_MEASURE,
    label: str = "knee",
) -> Callable[[int], ProbeResult]:
    """`probe_fn(rate) -> ProbeResult` para `run_saturation_search`.

    Espelha `run_measurement_battery.make_probe_fn` mas é escrito aqui, em
    vez de importado, por DOIS motivos: aquele monta o caminho remoto sob
    `_saturation/`, o namespace da campanha principal — manter esta campanha
    inteiramente sob `_estresse/` vale as ~20 linhas duplicadas; e o veredito
    de violação usa `ignore_latency_slo=True` (ver abaixo), que a bateria
    principal nunca deve receber. Os pedaços que importam
    (`build_remote_probe_rep_command`, `build_remote_probe_aggregate_command`,
    `_parse_probe_result`) são reusados verbatim, não recopiados.

    Uma repetição por patamar (`rep=0`, capturando /proc/stat nas duas
    pontas): a sondagem existe para ACHAR o joelho, não para reportá-lo com
    dispersão — a precisão vem depois, da fase fina da rampa construída em
    torno dele.

    `ignore_latency_slo=True` no `build_remote_probe_aggregate_command`
    abaixo: esta campanha quer o teto real do BANCO, não a SLO de latência
    de cliente (p99>200ms) que orienta a bateria principal. Achado ao vivo
    na 1ª sondagem de e4-valkey: CPU/memória agregados ficaram bem abaixo de
    qualquer teto quando o joelho "violou" por p99, porque o Valkey é
    single-thread no caminho de dados (~12,5% de CPU agregada numa VM de 8
    vCPU quando saturado — ver docs/DESIGN.md, "Valkey e o veredito
    automático") e o p99 do cliente pode estourar por fila em outra camada
    (serviço) antes do banco saturar de fato — o que subestimaria o joelho
    que esta campanha quer medir. Com `ignore_latency_slo=True`, só taxa de
    erro >1% (e o portão de vazão ofertada, nunca ignorado) decidem
    `violated_slo` aqui.

    `warmup`/`measure`/`label`: default para a sondagem do joelho
    (`PROBE_WARMUP`/`PROBE_MEASURE`/`"knee"`) — `--quick-probe`
    (`run_quick_probe`) passa `QUICK_PROBE_WARMUP`/`QUICK_PROBE_MEASURE`/
    `"quick"` em vez disso, mesma função, sem duplicar a closure inteira.
    `label` só distingue o prefixo do caminho remoto
    (`probe/{label}-N-rate`), para as duas sondagens nunca colidirem se
    rodadas na mesma campanha/timestamp.
    """
    counter = itertools.count()

    def probe_fn(rate: int) -> ProbeResult:
        probe_id = f"{label}-{next(counter)}-{rate}"
        remote_subdir = f"{ESTRESSE_PREFIX}/{cell_id}/{run_timestamp}/probe/{probe_id}"

        rep_cmd = build_remote_probe_rep_command(
            cell_id,
            target_url,
            tier,
            rate,
            warmup,
            measure,
            tools_image,
            RESULTS_MOUNT,
            FIXTURES_MOUNT,
            remote_subdir,
            0,
            capture_before_stat=True,
            capture_after_stat=True,
            # Sobe e apaga o k6-raw.json de cada sondagem: a 32k req/s ele
            # passa de 1 GB por patamar, e o disco precisa estar livre para
            # a NDJSON da rampa, que é o dado de verdade.
            results_bucket=results_bucket,
            user_count=user_count,
        )
        gcloud_ssh_with_retry(loadgen_instance, zone, project_id, rep_cmd)

        aggregate_cmd = build_remote_probe_aggregate_command(
            remote_subdir,
            1,
            rate,
            measure,
            tools_image,
            RESULTS_MOUNT,
            FIXTURES_MOUNT,
            # A campanha de estresse quer o teto real do BANCO, não a SLO de
            # latência do cliente (p99>200ms) — essa pode disparar por fila
            # em outra camada (serviço) antes do banco saturar de fato,
            # subestimando o joelho. Só a taxa de erro (>1%) e o portão de
            # vazão ofertada (nunca ignorado) decidem violated_slo aqui. Ver
            # analysis/probe_report.py:violated_slo().
            ignore_latency_slo=True,
        )
        result = gcloud_ssh_with_retry(loadgen_instance, zone, project_id, aggregate_cmd)
        verdict = _parse_probe_result(result.stdout)

        return ProbeResult(
            rate=rate,
            violated_slo=verdict.violated_slo,
            generator_cpu_percent=verdict.generator_cpu_percent,
            p99_ms=verdict.p99_ms,
            error_rate=verdict.error_rate,
            offered_ratio=verdict.offered_ratio,
            slo_throughput_rps=verdict.slo_throughput_rps,
        )

    return probe_fn


def knee_from_search(search) -> tuple[int | None, str]:
    """Traduz o resultado da busca no joelho a usar, ou explica por que não
    dá para usar.

    Os três desfechos não são equivalentes e nenhum pode virar um número
    silenciosamente:

    - `loadgen_bottleneck`: o GERADOR saturou antes da célula. A rampa
      inteira herdaria o mesmo teto e mediria o gerador — abortar é a única
      leitura honesta (mesma regra de docs/DESIGN.md para a campanha
      principal).
    - `censored`: nem no teto de 50.000 req/s houve violação. O joelho
      existe acima disso; usar `lower_bound` monta a rampa no lugar certo,
      mas quem lê precisa saber que o valor é um piso, não uma medida.
    - caso normal: `approx_throughput`.
    """
    if search.loadgen_bottleneck:
        return None, (
            "o gerador saturou durante a sondagem (CPU >= 60%): a rampa herdaria "
            "esse teto e mediria o gerador, não o banco. Escale --loadgen-machine-type."
        )
    if search.censored:
        return int(search.lower_bound), (
            f"censurado: nenhuma violação até o teto de {int(search.lower_bound)} req/s — "
            f"o joelho está ACIMA disso, e a rampa vai medir só o que couber abaixo."
        )
    if not search.approx_throughput:
        return None, "a sondagem não produziu vazão aproximada."
    return int(search.approx_throughput), ""


def build_proc_stat_sampler_command(duration_s: int, interval_s: int = PROC_STAT_INTERVAL_S) -> str:
    """Laço de amostragem em segundo plano na VM do banco.

    Contagem FIXA de iterações em vez de `while true` + `pkill`: a VM roda
    Container-Optimized OS, onde nem todo utilitário de processo está
    disponível, e um laço infinito que sobreviva ao fim do SSH vira lixo
    cobrando CPU se o encerramento falhar. Com contagem, o amostrador
    termina sozinho mesmo se a rampa abortar.

    Aritmética de shell POSIX pura (sem `seq`, sem `timeout`) pelo mesmo
    motivo: COS é mínimo.
    """
    iterations = max(1, duration_s // interval_s)
    loop = (
        f"i=0; while [ $i -lt {iterations} ]; do "
        f'echo "=== $(date -u +%Y-%m-%dT%H:%M:%S+00:00)"; '
        f"cat /proc/stat; "
        f"sleep {interval_s}; "
        f"i=$((i+1)); done"
    )
    return f"nohup sh -c {shlex.quote(loop)} > {PROC_STAT_REMOTE_PATH} 2>/dev/null &"


def build_remote_ramp_command(
    cell_id: str,
    target_url: str,
    tier: str,
    stages_json: str,
    max_vus: int,
    tools_image: str,
    results_mount: str,
    fixtures_mount: str,
    remote_subdir: str,
    *,
    user_count: int,
) -> str:
    """Uma execução contínua do k6 mais o relatório por degrau, na MESMA
    sessão remota.

    `analysis/ramp_report.py` roda aqui, na VM do gerador, e não no host, por
    topologia: é este lado que sobe seus resultados direto para o bucket
    (destino primário). A NDJSON bruta, que pode chegar a 28 GB, nunca
    precisa transitar.
    """
    run_dir = f"/app/results/{remote_subdir}"
    requests_out = f"{run_dir}/requests.ndjson"
    report_out = f"{run_dir}/ramp_{tier}.json"

    k6_argv = build_ramp_k6_cmd(
        Path(requests_out), cell_id, target_url, tier, stages_json, max_vus, user_count=user_count
    )
    report_argv = [
        "python",
        "-m",
        "analysis.ramp_report",
        requests_out,
        "--cell",
        cell_id,
        "--tier",
        tier,
        "--out",
        report_out,
        # Só a campanha de estresse chama analysis.ramp_report — sempre
        # ignora a SLO de latência, mesmo critério da sondagem do joelho
        # (make_stress_probe_fn). Ver analysis/probe_report.py:violated_slo().
        "--ignore-latency-slo",
    ]
    steps = [
        shlex.join(["mkdir", "-p", run_dir]),
        f"cat /proc/stat | head -1 > {run_dir}/gen_stat_before.txt",
        shlex.join(str(a) for a in k6_argv),
        f"cat /proc/stat | head -1 > {run_dir}/gen_stat_after.txt",
        shlex.join(report_argv),
    ]
    docker_argv = [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "-v",
        f"{results_mount}:/app/results",
        "-v",
        f"{fixtures_mount}:/app/load/fixtures:ro",
        "--entrypoint",
        "bash",
        tools_image,
        "-c",
        " && ".join(steps),
    ]
    return shlex.join(docker_argv)


def _memory_ceilings(service_machine: str, loadgen_machine: str) -> dict[str, float]:
    """Teto de memória (MB) por componente, para `classify_bottleneck` e para
    as colunas de memória em % das tabelas de sondagem/rampa. Espelha
    DEFAULT_MEMORY_MB_BY_COMPONENT da bateria principal, que assume
    n2-standard-8 em tudo — aqui serviço e gerador mudam por célula
    (CELL_DEFAULTS), então o teto tem de acompanhar ou tanto o veredito de
    gargalo quanto a % de memória saem errados. Chamada de main() e de
    run_quick_probe() — nunca recomputada separadamente, para as duas nunca
    poderem divergir sobre o que é 100% de memória para uma VM."""
    return {
        **DEFAULT_MEMORY_MB_BY_COMPONENT,
        "service": MACHINE_MEMORY_MB.get(service_machine, 32768.0),
        "loadgen": MACHINE_MEMORY_MB.get(loadgen_machine, 32768.0),
    }


def _nearest_sample_per_component(
    resource_samples: list[ResourceSample], at: datetime
) -> dict[str, ResourceSample | None]:
    """Para cada componente (database/service/loadgen), a amostra de
    resources.csv mais recente com timestamp <= `at`. `None` só nos
    primeiros patamares/degraus, antes da 1ª amostra do coletor em
    segundo plano (sample_resources_periodically) — nunca fabricado como
    zero, mesma disciplina de ProbeResult.generator_cpu_percent."""
    latest: dict[str, ResourceSample | None] = {"database": None, "service": None, "loadgen": None}
    for sample in resource_samples:
        if sample.timestamp is not None and sample.timestamp <= at:
            current = latest.get(sample.component)
            if current is None or sample.timestamp > current.timestamp:
                latest[sample.component] = sample
    return latest


def _memory_percent(sample: ResourceSample | None, memory_ceilings: dict[str, float]) -> str:
    if sample is None:
        return "—"
    ceiling = memory_ceilings.get(sample.component)
    if not ceiling:
        return "—"
    return f"{sample.memory_mb / ceiling * 100:.1f}%"


def _cpu_percent(sample: ResourceSample | None) -> str:
    if sample is None or sample.cpu_percent is None:
        return "—"
    return f"{sample.cpu_percent:.1f}%"


def format_quick_probe_table(
    sweep,
    level_timestamps: list[datetime],
    resource_samples: list[ResourceSample],
    memory_ceilings: dict[str, float],
) -> str:
    """Uma linha por patamar sondado por --quick-probe (load/saturation.py:
    LinearSweepResult), com CPU/memória das 3 VMs correlacionadas por
    timestamp via _nearest_sample_per_component. CPU do gerador vem de
    ProbeResult.generator_cpu_percent (leitura de /proc/stat específica
    daquele patamar, a mesma que o portão dos 60% usa) — nunca da amostra
    correlacionada, para a tabela nunca mostrar dois números diferentes
    para a mesma medição. Memória sempre em % do teto da VM (mesma base de
    classify_bottleneck), nunca MB bruto — ver docs/DESIGN.md.

    `vazão` = `offered_ratio * rate` — exato, não aproximado:
    ProbeResult.offered_ratio já é `request_count / (rate * duração)`
    (analysis/probe_report.py), então multiplicar por `rate` devolve
    exatamente `request_count / duração`, a vazão real sustentada naquele
    patamar. `oferta%` é o mesmo `offered_ratio` em percentual — abaixo de
    95% é o próprio portão de vazão ofertada (analysis/collect.py:
    MIN_OFFERED_RATIO) que pode ter violado o patamar mesmo com err%=0,00
    (ver docs/DESIGN.md, "Vazão ofertada verificada, não presumida") —
    mostrar os dois números lado a lado é o que permite distinguir essa
    causa de uma violação por taxa de erro.

    `vazãoSLO` = ProbeResult.slo_throughput_rps (analysis/probe_report.py:
    slo_throughput_rps) — requisições bem-sucedidas E dentro dos 200ms do
    SLO, por segundo. Distinto de `vazão` (toda requisição aceita, SLO ou
    não): a diferença entre as duas colunas é o que separa "o banco aceitou
    a carga" de "o banco respondeu dentro do prometido" — um patamar pode
    ter vazão alta e vazãoSLO baixa quando a fila cresce mas ainda não
    estoura maxVUs."""
    header = (
        f"  {'rate':>6}  {'vazão':>7}  {'oferta%':>8}  {'vazãoSLO':>8}  {'p99(ms)':>8}  {'err%':>6}  {'veredito':<8}  "
        f"{'cpuGer%':>8}  {'cpuSrv%':>8}  {'cpuBD%':>7}  "
        f"{'memGer%':>8}  {'memSrv%':>8}  {'memBD%':>7}"
    )
    sep = (
        f"  {'-' * 6}  {'-' * 7}  {'-' * 8}  {'-' * 8}  {'-' * 8}  {'-' * 6}  {'-' * 8}  "
        f"{'-' * 8}  {'-' * 8}  {'-' * 7}  "
        f"{'-' * 8}  {'-' * 8}  {'-' * 7}"
    )
    lines = [header, sep]
    for probe, at in zip(sweep.probes, level_timestamps):
        nearest = _nearest_sample_per_component(resource_samples, at)
        if probe.offered_ratio is not None:
            vazao = f"{probe.offered_ratio * probe.rate:.0f}"
            oferta = f"{probe.offered_ratio * 100:.1f}%"
        else:
            vazao = oferta = "—"
        vazao_slo = f"{probe.slo_throughput_rps:.0f}" if probe.slo_throughput_rps is not None else "—"
        p99 = f"{probe.p99_ms:.1f}" if probe.p99_ms is not None else "—"
        err = f"{probe.error_rate * 100:.2f}%" if probe.error_rate is not None else "—"
        veredito = "VIOLOU" if probe.violated_slo else "OK"
        cpu_ger = f"{probe.generator_cpu_percent:.1f}%" if probe.generator_cpu_percent is not None else "—"
        lines.append(
            f"  {probe.rate:>6}  {vazao:>7}  {oferta:>8}  {vazao_slo:>8}  {p99:>8}  {err:>6}  {veredito:<8}  "
            f"{cpu_ger:>8}  {_cpu_percent(nearest['service']):>8}  {_cpu_percent(nearest['database']):>7}  "
            f"{_memory_percent(nearest['loadgen'], memory_ceilings):>8}  "
            f"{_memory_percent(nearest['service'], memory_ceilings):>8}  "
            f"{_memory_percent(nearest['database'], memory_ceilings):>7}"
        )
    return "\n".join(lines)


def format_ramp_step_table(
    steps: list[dict],
    resource_samples: list[ResourceSample],
    memory_ceilings: dict[str, float],
) -> str:
    """Uma linha por degrau da rampa completa (report["steps"] de
    analysis/ramp_report.py:build_ramp_report, já achatado em dict pelo
    round-trip JSON), com CPU/memória das 3 VMs correlacionadas pelo
    `ended_at` de cada degrau — mesma correlação de format_quick_probe_table,
    mesma _nearest_sample_per_component, sem segundo algoritmo. Ao contrário
    da sondagem rápida, não há leitura de /proc/stat POR DEGRAU do gerador
    (o portão dos 60% da rampa só amostra uma vez, cercando a campanha
    inteira) — cpuGer% aqui também vem da amostra correlacionada, não de uma
    fonte mais precisa como na tabela da sondagem (ver docs/DESIGN.md).

    `vazãoSLO` = RampStepResult.slo_throughput_rps (analysis/ramp_report.py:
    step_results, via analysis/probe_report.py:slo_throughput_rps) —
    passagem direta, sem derivação: o dataframe bruto do degrau já existe
    em step_results antes de build_summary colapsá-lo em percentis. Mesmo
    significado da coluna homônima da tabela da sondagem rápida: goodput
    (status<400 E latência<=200ms), distinto de `vazão` (toda requisição
    aceita)."""
    header = (
        f"  {'rate':>6}  {'phase':<10}  {'vazão':>7}  {'oferta%':>8}  {'vazãoSLO':>8}  {'p50':>7}  {'p95':>7}  {'p99':>7}  {'err%':>5}  "
        f"{'oferta':<7}  {'veredito':<8}  {'cpuGer%':>8}  {'cpuSrv%':>8}  {'cpuBD%':>7}  "
        f"{'memGer%':>8}  {'memSrv%':>8}  {'memBD%':>7}"
    )
    sep = (
        f"  {'-' * 6}  {'-' * 10}  {'-' * 7}  {'-' * 8}  {'-' * 8}  {'-' * 7}  {'-' * 7}  {'-' * 7}  {'-' * 5}  "
        f"{'-' * 7}  {'-' * 8}  {'-' * 8}  {'-' * 8}  {'-' * 7}  "
        f"{'-' * 8}  {'-' * 8}  {'-' * 7}"
    )
    lines = [header, sep]
    for step in steps:
        ended_at = step.get("ended_at")
        at = datetime.fromisoformat(ended_at) if ended_at else None
        nearest = (
            _nearest_sample_per_component(resource_samples, at)
            if at is not None
            else {"database": None, "service": None, "loadgen": None}
        )
        vazao = f"{step['throughput_rps']:.0f}" if step.get("throughput_rps") is not None else "—"
        oferta_pct = f"{step['offered_ratio'] * 100:.1f}%" if step.get("offered_ratio") is not None else "—"
        vazao_slo = f"{step['slo_throughput_rps']:.0f}" if step.get("slo_throughput_rps") is not None else "—"
        p50 = f"{step['latency_ms_p50']:.1f}" if step.get("latency_ms_p50") is not None else "—"
        p95 = f"{step['latency_ms_p95']:.1f}" if step.get("latency_ms_p95") is not None else "—"
        p99 = f"{step['latency_ms_p99']:.1f}" if step.get("latency_ms_p99") is not None else "—"
        err = f"{step['error_rate'] * 100:.2f}%" if step.get("error_rate") is not None else "—"
        oferta = "OK" if step.get("offered_load_ok") else "DEFICIT"
        veredito = "VIOLOU" if step.get("violated_slo") else "OK"
        lines.append(
            f"  {step['rate']:>6}  {step['phase']:<10}  {vazao:>7}  {oferta_pct:>8}  {vazao_slo:>8}  {p50:>7}  {p95:>7}  {p99:>7}  {err:>5}  "
            f"{oferta:<7}  {veredito:<8}  "
            f"{_cpu_percent(nearest['loadgen']):>8}  {_cpu_percent(nearest['service']):>8}  "
            f"{_cpu_percent(nearest['database']):>7}  "
            f"{_memory_percent(nearest['loadgen'], memory_ceilings):>8}  "
            f"{_memory_percent(nearest['service'], memory_ceilings):>8}  "
            f"{_memory_percent(nearest['database'], memory_ceilings):>7}"
        )
    return "\n".join(lines)


def schedule_duration_s(schedule) -> int:
    return sum(step.duration_s for step in schedule)


def _quick_probe_top(explicit_top: int | None, knee: int, ceiling: int = CEILING_RPS) -> int:
    """Teto da sondagem linear rápida (--quick-probe-top): valor explícito, ou
    2x o joelho já resolvido (--knee ou CELL_DEFAULTS) — capado no teto global
    de load/saturation.py:CEILING_RPS, mesma disciplina da busca binária."""
    return min(ceiling, explicit_top or knee * 2)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cell", choices=sorted(CELL_DEFAULTS))
    parser.add_argument("project_id")
    parser.add_argument("region")
    parser.add_argument("zone")
    parser.add_argument("terraform_state_bucket")
    parser.add_argument("results_bucket")
    parser.add_argument("dataset_bucket")
    parser.add_argument("--service-image", required=True)
    parser.add_argument("--tools-image", required=True)
    parser.add_argument(
        "--knee",
        type=int,
        default=None,
        help="joelho projetado em req/s; default por célula (ver CELL_DEFAULTS)",
    )
    parser.add_argument("--fine-step", type=int, default=None, help="passo da fase fina em req/s")
    parser.add_argument(
        "--max-vus",
        type=int,
        default=None,
        help="teto de VUs do k6. Default: 20%% do topo da rampa — suficiente para o "
        "orçamento de latência do SLO com folga, sem a explosão de memória que a "
        "heurística RATE*2 de scenarios.js causaria.",
    )
    parser.add_argument(
        "--skip-probe",
        action="store_true",
        help="pula a sondagem do joelho e usa a projeção de CELL_DEFAULTS. A sondagem "
        "custa ~20 min contra as 2-3,4 h da rampa, e sem ela um erro de projeção de "
        "mais de 40%% para baixo desperdiça a campanha inteira. Passar --knee também "
        "pula a sondagem (valor explícito manda).",
    )
    parser.add_argument(
        "--quick-probe",
        action="store_true",
        help="roda uma varredura linear rápida em vez da sondagem do joelho + rampa "
        "completa: patamares de +--quick-probe-step req/s a cada 30s, subindo até a "
        "primeira violação (taxa de erro >1%%, mesmo critério --ignore-latency-slo da "
        "sondagem do joelho) ou até --quick-probe-top. Sem busca binária, sem "
        "cronograma multi-fase — minutos, não horas. Existe para checar rápido onde a "
        "célula falha e se o dimensionamento atual do serviço (CELL_DEFAULTS) deixa o "
        "BANCO aparecer como gargalo primeiro (ver db_cpu_cores.csv). Ignora "
        "--skip-probe/--knee/--fine-step/--max-vus: não há rampa para eles configurarem.",
    )
    parser.add_argument(
        "--quick-probe-step",
        type=int,
        default=1_000,
        help="incremento em req/s entre patamares da varredura linear (--quick-probe). "
        "Default 1.000, o mesmo grão da fase grossa da rampa completa.",
    )
    parser.add_argument(
        "--quick-probe-top",
        type=int,
        default=None,
        help="teto em req/s da varredura linear (--quick-probe); para antes disso se "
        "violar primeiro. Default: 2x o joelho projetado da célula (CELL_DEFAULTS ou "
        "--knee), capado no teto de load/saturation.py:CEILING_RPS.",
    )
    parser.add_argument("--service-machine-type", default=None)
    parser.add_argument("--loadgen-machine-type", default=None)
    parser.add_argument("--loadgen-boot-disk-gb", type=int, default=100)
    parser.add_argument("--user-count", type=int, default=MAIN_MEASUREMENT_USER_COUNT)
    parser.add_argument("--keep-infra", action="store_true")
    parser.add_argument("--yes", action="store_true", help="pula a confirmação de gasto")
    return parser.parse_args(argv)


def run_quick_probe(
    args,
    target_url: str,
    loadgen_instance: str,
    database_instance: str,
    outputs: dict,
    timestamp: str,
    remote_subdir: str,
    top: int,
    step: int,
    duration_estimate_s: int,
) -> int:
    """Corpo de --quick-probe: mesma amostragem de recursos (thread de
    resources.csv + /proc/stat por núcleo do banco) que a rampa completa já
    usa, mas roda load/saturation.py:run_linear_probe_sweep em vez de montar
    e disparar um cronograma k6. Extraído de main() (em vez de ramificado
    inline) porque main() já provisiona infraestrutura faturável e decide o
    destroy final — mesma razão de "Isolamento estrutural" no topo deste
    módulo: mais fácil revisar um corpo nomeado do que três ramos
    espalhados dentro de uma função de ~340 linhas."""
    defaults = CELL_DEFAULTS[args.cell]
    service_machine = args.service_machine_type or defaults["service"]
    loadgen_machine = args.loadgen_machine_type or defaults["loadgen"]
    memory_ceilings = _memory_ceilings(service_machine, loadgen_machine)

    instance_by_component = {
        "database": outputs["database_instance_id"],
        "service": outputs["service_instance_id"],
        "loadgen": outputs["loadgen_instance_id"],
    }
    resource_samples: list = []
    stop_event = threading.Event()
    sampler = threading.Thread(
        target=sample_resources_periodically,
        args=(
            make_resource_collect_fn(args.project_id, instance_by_component),
            stop_event,
            resource_samples,
        ),
        daemon=True,
    )
    sampler.start()

    # Amostrador por núcleo na VM do BANCO, em segundo plano — mesmo motivo
    # da rampa completa: sem ele, a saturação de um Valkey de thread única é
    # literalmente invisível no agregado.
    gcloud_ssh(
        database_instance,
        args.zone,
        args.project_id,
        build_proc_stat_sampler_command(duration_estimate_s),
    )

    print(
        f"\n--- sondagem linear rápida ({args.cell}, +{step} req/s a cada 30s, "
        f"até {top} req/s) ---"
    )
    level_timestamps: list[datetime] = []
    base_probe_fn = make_stress_probe_fn(
        args.cell,
        target_url,
        TIER,
        loadgen_instance,
        args.zone,
        args.project_id,
        args.tools_image,
        args.results_bucket,
        timestamp,
        user_count=args.user_count,
        warmup=QUICK_PROBE_WARMUP,
        measure=QUICK_PROBE_MEASURE,
        label="quick",
    )

    def timestamped_probe_fn(rate: int) -> ProbeResult:
        result = base_probe_fn(rate)
        # Registrado DEPOIS do probe_fn retornar: é o instante mais próximo
        # do fim real daquele patamar, para correlacionar com resources.csv.
        level_timestamps.append(datetime.now(timezone.utc))
        return result

    sweep = run_linear_probe_sweep(
        timestamped_probe_fn, start_rate=PROBE_START_RATE, step=step, ceiling=top
    )

    stop_event.set()
    sampler.join(timeout=30)

    host_tmp = Path("results") / ".tmp-estresse" / args.cell / timestamp
    host_tmp.mkdir(parents=True, exist_ok=True)

    resources_csv = host_tmp / "resources.csv"
    write_resources_csv(resource_samples, resources_csv)

    proc_stat_log = gcloud_ssh(
        database_instance, args.zone, args.project_id, f"cat {PROC_STAT_REMOTE_PATH}"
    )
    core_samples = samples_from_log(proc_stat_log.stdout or "")
    db_cpu_csv = host_tmp / "db_cpu_cores.csv"
    write_db_cpu_cores_csv(core_samples, db_cpu_csv)
    peak = max_core_percent(core_samples)

    try:
        bottleneck = classify_bottleneck(resource_samples, memory_ceiling_mb=memory_ceilings)
    except ValueError:
        bottleneck = None

    print()
    print(format_quick_probe_table(sweep, level_timestamps, resource_samples, memory_ceilings))
    print()
    if sweep.loadgen_bottleneck:
        print(
            "AVISO: o gerador saturou (CPU >= 60%) antes de qualquer violação da célula "
            "— sondagem inválida como dado do banco. Escale --loadgen-machine-type."
        )
    elif sweep.censored:
        print(f"AVISO: nenhuma violação até o teto de {top} req/s — o joelho está ACIMA disso.")
    else:
        last = sweep.probes[-1]
        print(
            f"Primeira violação: {last.rate} req/s "
            f"(error_rate={last.error_rate}, p99={last.p99_ms}ms)"
        )

    if bottleneck is not None:
        print(f"Gargalo dominante (agregado, resources.csv): {bottleneck}")
    else:
        print("AVISO: sem amostra de CPU suficiente para classificar o gargalo agregado.")
    if peak is not None:
        print(f"CPU máxima de um núcleo do banco (db_cpu_cores.csv): {peak:.1f}%")
        if bottleneck is not None and not bottleneck.startswith("database"):
            print(
                f"AVISO: núcleo do banco chegou a {peak:.1f}% mesmo com o gargalo agregado "
                f"apontando para '{bottleneck}' — típico de saturação de thread única "
                "(ex.: Valkey), que o agregado de resources.csv não enxerga. Não descarte "
                "o banco como candidato ao limite real só pelo veredito agregado."
            )

    probe_dicts = []
    for probe, at in zip(sweep.probes, level_timestamps):
        nearest = _nearest_sample_per_component(resource_samples, at)
        probe_dicts.append(
            {
                **asdict(probe),
                "service_cpu_percent": nearest["service"].cpu_percent if nearest["service"] else None,
                "database_cpu_percent": nearest["database"].cpu_percent
                if nearest["database"]
                else None,
                "generator_memory_mb": nearest["loadgen"].memory_mb if nearest["loadgen"] else None,
                "service_memory_mb": nearest["service"].memory_mb if nearest["service"] else None,
                "database_memory_mb": nearest["database"].memory_mb
                if nearest["database"]
                else None,
            }
        )

    quick_probe_json = host_tmp / "quick_probe.json"
    quick_probe_json.write_text(
        json.dumps(
            {
                "loadgen_bottleneck": sweep.loadgen_bottleneck,
                "censored": sweep.censored,
                "generator_cpu_unmeasured": sweep.generator_cpu_unmeasured,
                "bottleneck": bottleneck,
                "db_peak_core_percent": peak,
                "probes": probe_dicts,
            },
            indent=2,
        )
    )
    quick_probe_table_txt = host_tmp / "quick_probe_table.txt"
    quick_probe_table_txt.write_text(
        format_quick_probe_table(sweep, level_timestamps, resource_samples, memory_ceilings),
        # Sem isto, Path.write_text() usa a codificação padrão do locale do
        # host — cp1252 num host Windows — e corrompe permanentemente "ã"/"õ"
        # dos cabeçalhos ("vazão", "oferta%") no arquivo salvo: não é só a
        # exibição no console (isso é só o codepage do terminal), os BYTES
        # gravados ficam errados, e sobem assim para o GCS. Confirmado ao
        # vivo: quick_probe_table.txt salvo tinha 0xE3 (cp1252 de "ã") em vez
        # de 0xC3 0xA3 (UTF-8).
        encoding="utf-8",
    )

    for path in (resources_csv, db_cpu_csv, quick_probe_json, quick_probe_table_txt):
        _run(
            [
                "gcloud",
                "storage",
                "cp",
                str(path),
                f"gs://{args.results_bucket}/{remote_subdir}/{path.name}",
            ]
        )
        path.unlink()

    gcloud_ssh(
        loadgen_instance,
        args.zone,
        args.project_id,
        build_remote_upload_command(
            f"/app/results/{remote_subdir}",
            RESULTS_MOUNT,
            args.results_bucket,
            remote_subdir,
            args.tools_image,
        ),
    )

    print(
        f"\nResultados em gs://{args.results_bucket}/{remote_subdir}/\n"
        f"Baixe com: python -m analysis.fetch_results {args.results_bucket}"
    )
    return 0


def main(argv=None) -> int:
    args = _parse_args(argv)
    defaults = CELL_DEFAULTS[args.cell]
    knee = args.knee or defaults["knee"]
    fine_step = args.fine_step or defaults["fine_step"]
    service_machine = args.service_machine_type or defaults["service"]
    loadgen_machine = args.loadgen_machine_type or defaults["loadgen"]

    disk_budget_bytes = args.loadgen_boot_disk_gb * 1_000_000_000

    def plan_ramp(knee_value: int) -> tuple[list, int, int, int]:
        sched = build_step_schedule(knee_value, fine_step)
        top = max(s.rate for s in sched)
        vus = args.max_vus or max(1_000, int(top * 0.2))
        # Pré-voo de disco: a rampa não grava k6-raw.json, mas a NDJSON por
        # requisição sozinha chega a dezenas de GB. Falhar aqui custa zero;
        # descobrir depois custa a campanha (NDJSON truncado, parse quebrado).
        check_disk_budget(sched, disk_budget_bytes)
        return sched, top, vus, schedule_duration_s(sched)

    schedule = top_rate = max_vus = duration_s = None
    quick_probe_top = quick_probe_duration_estimate_s = None
    probing = False

    if args.quick_probe:
        if args.quick_probe_step <= 0:
            print(f"ERRO: --quick-probe-step precisa ser positivo, recebeu {args.quick_probe_step}.")
            return 1
        quick_probe_top = _quick_probe_top(args.quick_probe_top, knee)
        if quick_probe_top < PROBE_START_RATE:
            print(
                f"ERRO: --quick-probe-top ({quick_probe_top}) é menor que o patamar "
                f"inicial da sondagem ({PROBE_START_RATE})."
            )
            return 1
        levels = (quick_probe_top - PROBE_START_RATE) // args.quick_probe_step + 1
        # 45s de folga por patamar (30s de medição + latência das duas sessões
        # SSH por nível, cada uma com retry), não 30s — sem essa folga o
        # amostrador de /proc/stat do banco (contagem FIXA de iterações) pode
        # expirar antes do último patamar, deixando os degraus finais sem
        # leitura de CPU por núcleo.
        quick_probe_duration_estimate_s = levels * 45 + PROC_STAT_MARGIN_S
        print(
            f"Sondagem linear rápida: +{args.quick_probe_step} req/s a cada 30s, até "
            f"{quick_probe_top} req/s (~{levels} patamares, "
            f"~{quick_probe_duration_estimate_s / 60:.0f} min)."
        )
    else:
        # Provisório, a partir da projeção: serve de sanidade antes de
        # provisionar qualquer VM e de estimativa no aviso de gasto. Se a
        # sondagem rodar, este cronograma é descartado pelo medido.
        schedule, top_rate, max_vus, duration_s = plan_ramp(knee)
        probing = not args.skip_probe and args.knee is None
        print(
            f"Rampa (projetada, joelho={knee}): {len(schedule)} degraus, topo em "
            f"{top_rate} req/s, ~{duration_s / 60:.0f} min de carga, maxVUs={max_vus}."
        )
        if probing:
            print("Sondagem do joelho ATIVA: o cronograma acima será refeito sobre o valor medido.")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    remote_subdir = f"{ESTRESSE_PREFIX}/{args.cell}/{timestamp}"
    local_dir = Path("results") / ESTRESSE_PREFIX / args.cell / timestamp
    if local_dir.exists():
        print(f"ERRO: {local_dir} já existe. Esta campanha nunca sobrescreve resultado.")
        return 1

    storage = storage_for_cell(args.cell)
    snapshot_name = f"tcc-dataset-seed-{storage}"
    snapshot_found = storage != "valkey" and snapshot_exists(args.project_id, snapshot_name)

    applied = False
    tf_vars: list[str] = []
    try:
        if args.quick_probe:
            billable_msg = (
                f"terraform apply da campanha de ESTRESSE de '{args.cell}' (sondagem "
                f"linear rápida) em {args.project_id}/{args.region} vai criar 3 VMs reais "
                f"— serviço em {service_machine} e gerador em {loadgen_machine}, bem "
                f"maiores que o padrão — e rodar ~{quick_probe_duration_estimate_s / 60:.0f} "
                f"min de sondagem, cobrando o tempo todo. Estado e nomes de recurso são "
                f"separados da campanha principal."
            )
        else:
            billable_msg = (
                f"terraform apply da campanha de ESTRESSE de '{args.cell}' em "
                f"{args.project_id}/{args.region} vai criar 3 VMs reais — serviço em "
                f"{service_machine} e gerador em {loadgen_machine}, bem maiores que o padrão — "
                f"e rodar ~{duration_s / 60:.0f} min de carga contínua, cobrando o tempo todo. "
                f"Estado e nomes de recurso são separados da campanha principal."
            )
        _confirm_billable(billable_msg, auto_approve=args.yes)
        applied = True

        print("Mintando token de acesso via impersonação da SA do Terraform (nunca fica em disco)...")
        os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = fetch_terraform_access_token(args.project_id)

        terraform(
            [
                "init",
                "-reconfigure",
                f"-backend-config=bucket={args.terraform_state_bucket}",
                # `estresse/`, nunca `cells/`: o estado da campanha principal
                # não pode ser aberto por esta execução.
                f"-backend-config=prefix=estresse/{args.cell}",
            ],
            tf_dir=TF_DIR,
            cell=f"estresse-{args.cell}",
        )
        tf_vars = [
            f"-var=project_id={args.project_id}",
            f"-var=region={args.region}",
            f"-var=zone={args.zone}",
            f"-var=cell={args.cell}",
            f"-var=storage={storage}",
            f"-var=service_image={args.service_image}",
            f"-var=tools_image={args.tools_image}",
            f"-var=dataset_bucket={args.dataset_bucket}",
            f"-var=results_bucket={args.results_bucket}",
            f"-var=service_machine_type={service_machine}",
            f"-var=loadgen_machine_type={loadgen_machine}",
            f"-var=loadgen_boot_disk_gb={args.loadgen_boot_disk_gb}",
        ]
        if snapshot_found:
            tf_vars.append(f"-var=data_disk_snapshot={snapshot_name}")
        terraform(["apply", "-auto-approve", *tf_vars], tf_dir=TF_DIR, cell=f"estresse-{args.cell}")

        outputs = terraform_output_json(tf_dir=TF_DIR, cell=f"estresse-{args.cell}")
        database_ip = outputs["database_internal_ip"]
        service_ip = outputs["service_internal_ip"]
        # Sufixo "-st" nos nomes: mesmo local.name_suffix de
        # infra/envs/estresse/main.tf. Sem isto o gcloud ssh miraria as VMs
        # da campanha principal.
        database_instance = f"tcc-{args.cell}-database-st"
        service_instance = f"tcc-{args.cell}-service-st"
        loadgen_instance = f"tcc-{args.cell}-loadgen-st"

        wait_for_container(database_instance, args.zone, args.project_id, "tcc-database")
        wait_for_container(service_instance, args.zone, args.project_id, "tcc-service")

        postgres_password = None
        if storage == "postgres":
            secret = _run(
                [
                    "gcloud",
                    "secrets",
                    "versions",
                    "access",
                    "latest",
                    "--secret=tcc-postgres-password",
                    f"--project={args.project_id}",
                ],
                capture_output=True,
                text=True,
            )
            postgres_password = secret.stdout.strip()

        gcloud_ssh(
            loadgen_instance,
            args.zone,
            args.project_id,
            build_remote_setup_command(
                args.cell,
                storage,
                database_ip,
                service_ip,
                args.tools_image,
                postgres_password,
                FIXTURES_MOUNT,
                args.dataset_bucket,
                skip_dataset_load=snapshot_found,
            ),
        )
        if not snapshot_found:
            restart_container(service_instance, args.zone, args.project_id, "tcc-service")
            wait_for_container(service_instance, args.zone, args.project_id, "tcc-service")
        wait_for_service_ready(loadgen_instance, args.zone, args.project_id, service_ip)
        target_url = f"http://{service_ip}:8000/v1/recommendations"

        if args.quick_probe:
            return run_quick_probe(
                args,
                target_url,
                loadgen_instance,
                database_instance,
                outputs,
                timestamp,
                remote_subdir,
                quick_probe_top,
                args.quick_probe_step,
                quick_probe_duration_estimate_s,
            )

        # --- sondagem rápida do joelho ----------------------------------
        # Uma repetição por patamar, patamares em dobra, busca binária para
        # refinar. O objetivo é ENQUADRAR o joelho, não reportá-lo com
        # dispersão — a precisão vem da fase fina da rampa montada em torno
        # dele. Sem isto, um erro de projeção de 40% desperdiça a campanha
        # inteira (ver a docstring das constantes PROBE_*).
        if probing:
            print("\n--- sondagem do joelho (1 repetição por patamar) ---")
            search = run_saturation_search(
                make_stress_probe_fn(
                    args.cell,
                    target_url,
                    TIER,
                    loadgen_instance,
                    args.zone,
                    args.project_id,
                    args.tools_image,
                    args.results_bucket,
                    timestamp,
                    user_count=args.user_count,
                ),
                start_rate=PROBE_START_RATE,
                step_mode=PROBE_STEP_MODE,
                confirm_repetitions=0,
            )
            probe_path = Path("results") / ".tmp-estresse" / args.cell / timestamp
            probe_path.mkdir(parents=True, exist_ok=True)
            # Nome fora do glob `saturation*.json` de analysis/report.py: é
            # uma sondagem exploratória desta campanha, nunca o S que
            # alimenta n(D) = ⌈D/S⌉.
            (probe_path / "knee_probe.json").write_text(
                json.dumps(
                    {
                        "approx_throughput": search.approx_throughput,
                        "censored": search.censored,
                        "lower_bound": search.lower_bound,
                        "loadgen_bottleneck": search.loadgen_bottleneck,
                        "generator_cpu_unmeasured": search.generator_cpu_unmeasured,
                        "probes": [asdict(p) for p in search.probes],
                    },
                    indent=2,
                )
            )

            measured, note = knee_from_search(search)
            if measured is None:
                print(f"ERRO: {note}")
                return 1
            if note:
                print(f"AVISO: {note}")

            print(f"Joelho medido: {measured} req/s (projetado era {knee}).")
            knee = measured
            schedule, top_rate, max_vus, duration_s = plan_ramp(knee)
            print(
                f"Rampa (medida): {len(schedule)} degraus, topo em {top_rate} req/s, "
                f"~{duration_s / 60:.0f} min de carga, maxVUs={max_vus}."
            )

        instance_by_component = {
            "database": outputs["database_instance_id"],
            "service": outputs["service_instance_id"],
            "loadgen": outputs["loadgen_instance_id"],
        }
        memory_ceilings = _memory_ceilings(service_machine, loadgen_machine)

        resource_samples: list = []
        stop_event = threading.Event()
        sampler = threading.Thread(
            target=sample_resources_periodically,
            args=(
                make_resource_collect_fn(args.project_id, instance_by_component),
                stop_event,
                resource_samples,
            ),
            daemon=True,
        )
        sampler.start()

        # Amostrador por núcleo na VM do BANCO, em segundo plano. Sem ele, a
        # saturação de um Valkey de thread única é literalmente invisível.
        gcloud_ssh(
            database_instance,
            args.zone,
            args.project_id,
            build_proc_stat_sampler_command(duration_s + PROC_STAT_MARGIN_S),
        )

        print(f"\n--- rampa de estresse ({args.cell}, seletividade {TIER}) ---")
        stages_json = json.dumps([asdict(s) for s in schedule], separators=(",", ":"))
        report_path = f"/app/results/{remote_subdir}/ramp_{TIER}.json"
        ramp_result = gcloud_ssh_with_retry(
            loadgen_instance,
            args.zone,
            args.project_id,
            build_remote_ramp_command(
                args.cell,
                target_url,
                TIER,
                stages_json,
                max_vus,
                args.tools_image,
                RESULTS_MOUNT,
                FIXTURES_MOUNT,
                remote_subdir,
                user_count=args.user_count,
            ),
        )
        # O RAMP_REPORT/eventual AVISO que analysis/ramp_report.py já imprime
        # na VM do gerador ficava só no stdout capturado, nunca chegava ao
        # console de quem roda esta campanha — só o JSON baixado depois.
        print("\n--- resumo da rampa (analysis/ramp_report.py, na VM do gerador) ---")
        print((ramp_result.stdout or "").strip())

        stop_event.set()
        sampler.join(timeout=30)

        # --- artefatos que nascem no host -------------------------------
        # resources.csv e db_cpu_cores.csv não podem nascer na VM do banco
        # (cuja service account não tem permissão de escrita no bucket).
        # Vão para um diretório temporário, sobem, e a cópia local legítima
        # vem depois do download — GCS primeiro, local depois.
        host_tmp = Path("results") / ".tmp-estresse" / args.cell / timestamp
        host_tmp.mkdir(parents=True, exist_ok=True)

        resources_csv = host_tmp / "resources.csv"
        write_resources_csv(resource_samples, resources_csv)
        print(f"Tetos de memória usados na classificação de gargalo: {memory_ceilings}")

        try:
            bottleneck = classify_bottleneck(resource_samples, memory_ceiling_mb=memory_ceilings)
            print(f"Gargalo dominante (agregado, resources.csv): {bottleneck}")
        except ValueError:
            bottleneck = None
            print("AVISO: sem amostra de CPU suficiente para classificar o gargalo agregado.")

        # gcloud_ssh já roda com capture_output=True/text=True e devolve o
        # CompletedProcess — stdout sai pronto, sem kwarg extra.
        proc_stat_log = gcloud_ssh(
            database_instance,
            args.zone,
            args.project_id,
            f"cat {PROC_STAT_REMOTE_PATH}",
        )
        core_samples = samples_from_log(proc_stat_log.stdout or "")
        db_cpu_csv = host_tmp / "db_cpu_cores.csv"
        write_db_cpu_cores_csv(core_samples, db_cpu_csv)

        peak = max_core_percent(core_samples)
        if peak is not None:
            print(f"CPU máxima de UM núcleo do banco durante a rampa: {peak:.1f}%")
            if bottleneck is not None and not bottleneck.startswith("database"):
                print(
                    f"AVISO: núcleo do banco chegou a {peak:.1f}% mesmo com o gargalo "
                    f"agregado apontando para '{bottleneck}' — típico de saturação de "
                    "thread única (ex.: Valkey), que o agregado de resources.csv não "
                    "enxerga. Não descarte o banco como candidato ao limite real só "
                    "pelo veredito agregado."
                )

        # Busca o próprio ramp_<tier>.json de volta (mesmo padrão de "cat" já
        # usado acima para /proc/stat) — é o único jeito de correlacionar cada
        # degrau com resources.csv, que só existe aqui no HOST, depois que a
        # sessão SSH da rampa (que gerou o JSON, do lado do gerador) retornou.
        report_result = gcloud_ssh(loadgen_instance, args.zone, args.project_id, f"cat {report_path}")
        report = json.loads(report_result.stdout)
        step_table = format_ramp_step_table(report["steps"], resource_samples, memory_ceilings)
        print("\n--- degraus da rampa (com uso de recursos por VM) ---")
        print(step_table)
        step_table_path = host_tmp / f"ramp_{TIER}_table.txt"
        # encoding="utf-8" pelo mesmo motivo de quick_probe_table_txt acima —
        # sem isto, o padrão do locale do host (cp1252 no Windows) corrompe
        # "ã"/"õ" nos cabeçalhos permanentemente no arquivo salvo.
        step_table_path.write_text(step_table, encoding="utf-8")

        for path in (resources_csv, db_cpu_csv, step_table_path):
            _run(
                [
                    "gcloud",
                    "storage",
                    "cp",
                    str(path),
                    f"gs://{args.results_bucket}/{remote_subdir}/{path.name}",
                ]
            )
            # Removido do temporário de propósito: a cópia local vem do
            # download (analysis/fetch_results.py), nunca de um resquício
            # que poderia divergir do que está no bucket.
            path.unlink()

        gcloud_ssh(
            loadgen_instance,
            args.zone,
            args.project_id,
            build_remote_upload_command(
                f"/app/results/{remote_subdir}",
                RESULTS_MOUNT,
                args.results_bucket,
                remote_subdir,
                args.tools_image,
            ),
        )

        print(
            f"\nResultados em gs://{args.results_bucket}/{remote_subdir}/\n"
            f"Baixe com: python -m analysis.fetch_results {args.results_bucket}"
        )
        return 0

    finally:
        if applied and not args.keep_infra:
            _confirm_billable(
                f"terraform destroy da campanha de estresse de '{args.cell}'.",
                auto_approve=args.yes,
            )
            # Reminta o token: dura só ~1h, e a sondagem + rampa somadas passam
            # disso com folga (ver mesmo problema e correção em
            # run_measurement_battery.py) — sem isto o destroy final falha com
            # "Error 401: invalid authentication credentials", deixando as 3 VMs
            # (maiores que o padrão nesta campanha) presas cobrando.
            print("Remintando token de acesso antes do destroy (o anterior pode ter expirado)...")
            os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = fetch_terraform_access_token(args.project_id)
            terraform(
                ["destroy", "-auto-approve", *tf_vars],
                tf_dir=TF_DIR,
                cell=f"estresse-{args.cell}",
            )
        elif applied:
            print(
                f"\n--keep-infra: as 3 VMs de '{args.cell}-st' CONTINUAM DE PÉ e cobrando. "
                f"Destrua com terraform -chdir={TF_DIR} destroy."
            )


if __name__ == "__main__":
    sys.exit(main())
