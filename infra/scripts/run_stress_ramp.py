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
from analysis.resources import write_resources_csv
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
from load.saturation import ProbeResult, run_saturation_search

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
) -> Callable[[int], ProbeResult]:
    """`probe_fn(rate) -> ProbeResult` para `run_saturation_search`.

    Espelha `run_measurement_battery.make_probe_fn` mas é escrito aqui, em
    vez de importado, por UM motivo: aquele monta o caminho remoto sob
    `_saturation/`, o namespace da campanha principal. Manter esta campanha
    inteiramente sob `_estresse/` vale as ~20 linhas — e os pedaços que
    importam (`build_remote_probe_rep_command`,
    `build_remote_probe_aggregate_command`, `_parse_probe_result`) são
    reusados verbatim, não recopiados.

    Uma repetição por patamar (`rep=0`, capturando /proc/stat nas duas
    pontas): a sondagem existe para ACHAR o joelho, não para reportá-lo com
    dispersão — a precisão vem depois, da fase fina da rampa construída em
    torno dele.
    """
    counter = itertools.count()

    def probe_fn(rate: int) -> ProbeResult:
        probe_id = f"knee-{next(counter)}-{rate}"
        remote_subdir = f"{ESTRESSE_PREFIX}/{cell_id}/{run_timestamp}/probe/{probe_id}"

        rep_cmd = build_remote_probe_rep_command(
            cell_id,
            target_url,
            tier,
            rate,
            PROBE_WARMUP,
            PROBE_MEASURE,
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
            PROBE_MEASURE,
            tools_image,
            RESULTS_MOUNT,
            FIXTURES_MOUNT,
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


def schedule_duration_s(schedule) -> int:
    return sum(step.duration_s for step in schedule)


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
    parser.add_argument("--service-machine-type", default=None)
    parser.add_argument("--loadgen-machine-type", default=None)
    parser.add_argument("--loadgen-boot-disk-gb", type=int, default=100)
    parser.add_argument("--user-count", type=int, default=MAIN_MEASUREMENT_USER_COUNT)
    parser.add_argument("--keep-infra", action="store_true")
    parser.add_argument("--yes", action="store_true", help="pula a confirmação de gasto")
    return parser.parse_args(argv)


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

    # Provisório, a partir da projeção: serve de sanidade antes de provisionar
    # qualquer VM e de estimativa no aviso de gasto. Se a sondagem rodar, este
    # cronograma é descartado pelo medido.
    schedule, top_rate, max_vus, duration_s = plan_ramp(knee)
    probing = not args.skip_probe and args.knee is None
    print(
        f"Rampa (projetada, joelho={knee}): {len(schedule)} degraus, topo em {top_rate} req/s, "
        f"~{duration_s / 60:.0f} min de carga, maxVUs={max_vus}."
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
        _confirm_billable(
            f"terraform apply da campanha de ESTRESSE de '{args.cell}' em "
            f"{args.project_id}/{args.region} vai criar 3 VMs reais — serviço em "
            f"{service_machine} e gerador em {loadgen_machine}, bem maiores que o padrão — "
            f"e rodar ~{duration_s / 60:.0f} min de carga contínua, cobrando o tempo todo. "
            f"Estado e nomes de recurso são separados da campanha principal.",
            auto_approve=args.yes,
        )
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
        memory_ceilings = {
            **DEFAULT_MEMORY_MB_BY_COMPONENT,
            "service": MACHINE_MEMORY_MB.get(service_machine, 32768.0),
            "loadgen": MACHINE_MEMORY_MB.get(loadgen_machine, 32768.0),
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
        gcloud_ssh_with_retry(
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

        for path in (resources_csv, db_cpu_csv):
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
