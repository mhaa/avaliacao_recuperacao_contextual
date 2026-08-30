#!/usr/bin/env python3
"""Bateria de medição real em nuvem — README.md, "Fase 5". Contraparte de
infra/scripts/cloud_smoke_test.py (Fase 4): em vez de um smoke curto, roda
load/run_battery.py de verdade contra a célula, varrendo carga ×
seletividade conforme CONTEXTO.md ("Protocolo de medição" /
"Delineamento em duas etapas"), e traz os `results/` de volta. Também roda
a busca de vazão de saturação (load/saturation.py) — a 3ª dimensão da
fronteira de Pareto: rampa curta exploratória na triagem (1 patamar por
combinação, busca binária ao violar o SLO, teto de 50.000 req/s), rampa
fina de confirmação (5 repetições por patamar, incrementos de 10%) só nas
células não dominadas.

Roda no HOST (não dentro do container `tools`), pelo mesmo motivo de
cloud_smoke_test.py: a imagem `tools` não tem o `gcloud` CLI, só
terraform+k6+Python — e agora também precisa de `google-cloud-monitoring`
no host, para `analysis.resources.GCPMonitoringCollector` checar a CPU do
gerador a cada patamar da busca de saturação (README.md, "Instrumentação
de gargalo"). Terraform roda via docker-compose.gcp.yml; a bateria de
verdade e as sondagens de saturação rodam dentro de `docker run --rm
--network host <tools_image> ...` executados remotamente via `gcloud
compute ssh <loadgen> --tunnel-through-iap` — os IPs vêm de `terraform
output -json`, a VM de serviço não tem IP público.

Uso (via `-m`, não como caminho de arquivo direto — este módulo importa
infra.scripts.cloud_smoke_test por nome absoluto, e `infra/` só existe no
container por bind mount, nunca copiado pela imagem; `-m` garante que /app
entra no sys.path, `python infra/scripts/run_measurement_battery.py` não):
    python -m infra.scripts.run_measurement_battery <cell> <project-id> <region> <zone> \\
        <terraform-state-bucket> <results-bucket> <dataset-bucket> --phase triagem \\
        [--repetitions 5] [--seed 42] [--verify-otel] [--keep-infra]

    # --verify-otel: só na primeira célula da triagem — confirma que o
    # coletor OpenTelemetry das 3 VMs está exportando métricas de verdade
    # antes de rodar as outras 13 (o módulo Terraform é idêntico nas 14).

    # confirmação: --saturation-start vem do report.json da triagem
    # (aproximado ou lower_bound se a célula ficou censurada). resources.csv
    # é escrito nesta fase (amostragem periódica de 5s durante o sweep +
    # rampa fina, CONTEXTO.md), nunca na triagem.
    python -m infra.scripts.run_measurement_battery <cell> ... --phase confirmacao \\
        --saturation-start 11000

Cada comando faturável (terraform apply/destroy) é anunciado explicitamente
antes de rodar e pede confirmação — mesma regra permanente de
cloud_smoke_test.py, pedida pelo usuário.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import random
import shlex
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from infra.scripts.cloud_smoke_test import (
    _confirm_billable,
    _run,
    build_schema_and_fixture_steps,
    build_storage_env_flags,
    fetch_terraform_access_token,
    gcloud_ssh,
    resource_snapshot,
    storage_for_cell,
    terraform,
    terraform_output_json,
    wait_for_container,
)
from load.run_battery import build_probe_k6_cmd
from load.saturation import GENERATOR_CPU_THRESHOLD, ProbeResult, run_saturation_search

# CONTEXTO.md, "Protocolo de medição".
RATES = [100, 1_000, 10_000]
SELECTIVITY_TIERS = ["high", "medium", "low"]
# CONTEXTO.md, "Delineamento em duas etapas": a triagem roda com seletividade
# e carga fixas em nível intermediário, para achar a fronteira de Pareto;
# só a confirmação varre tudo.
TRIAGEM_RATE = 1_000
TRIAGEM_TIER = "medium"
REPETITIONS = 5
# Diretórios no disco da VM loadgen (fora do container) — precisam
# sobreviver ao `--rm` de cada `docker run`. `RESULTS_MOUNT` porque
# `results/` sumiria quando o container sai; `FIXTURES_MOUNT` porque
# `load/export_contexts_by_tier.py` roda no setup (um `docker run`) e
# `load/scenarios.js` lê `load/fixtures/contexts_by_tier.json` em cada
# combinação (outro `docker run`, container novo) — sem o bind mount, o
# arquivo gerado no setup não chegaria lá.
RESULTS_MOUNT = "/home/tcc/results"
FIXTURES_MOUNT = "/home/tcc/load-fixtures"

# Repetições por patamar na rampa de confirmação (CONTEXTO.md, item pedido
# pelo usuário) — a rampa curta usa 1 (ensaio único, por isso a tolerância
# de 20% em analysis/pareto.py).
CONFIRMATION_REPETITIONS = 5
CONFIRMATION_WARMUP = "2m"
CONFIRMATION_MEASURE = "3m"
SHORT_RAMP_WARMUP = "0s"
SHORT_RAMP_MEASURE = "1m"

# IMPLEMENTACAO.md, "Topologia": memória nominal dos tipos de máquina
# padrão — só para converter a fração que o coletor OpenTelemetry reporta
# em MB (analysis/resources.py:GCPMonitoringCollector), sem uma chamada
# extra à API do Compute para descobrir o tipo de máquina em runtime. Se
# `machine_type` for sobrescrito em terraform.tfvars, ajustar aqui também.
DEFAULT_MEMORY_MB_BY_COMPONENT = {
    "database": 32768.0,  # n2-standard-8
    "service": 16384.0,  # n2-standard-4
    "loadgen": 32768.0,  # n2-standard-8
}

# Janela de amostragem periódica de recursos durante a rampa de
# confirmação (CONTEXTO.md: "amostrar a cada 5 segundos nas três VMs").
RESOURCE_SAMPLE_INTERVAL_SECONDS = 5


def build_sweep(phase: str) -> list[tuple[int, str]]:
    if phase == "triagem":
        return [(TRIAGEM_RATE, TRIAGEM_TIER)]
    if phase == "confirmacao":
        return [(rate, tier) for rate in RATES for tier in SELECTIVITY_TIERS]
    raise ValueError(f"fase desconhecida: {phase!r}")


def shuffled_sweep(sweep: list[tuple[int, str]], seed: int) -> list[tuple[int, str]]:
    """Embaralha a ordem das combinações (carga, seletividade) rodadas na
    mesma VM — mesmo motivo de load/run_battery.py:shuffled_cell_order (cache
    do SO/throttling térmico acumulado enviesando sempre a mesma combinação).
    Diferente de lá, a ordem *entre células* não precisa mais ser embaralhada
    aqui: cada célula já roda numa VM nova e isolada (apply/destroy por
    célula), sem estado compartilhado entre elas."""
    order = list(sweep)
    random.Random(seed).shuffle(order)
    return order


def build_remote_setup_command(
    cell_id: str,
    storage: str,
    database_ip: str,
    service_ip: str,
    tools_image: str,
    postgres_password: str | None,
    fixtures_mount: str,
    dataset_bucket: str,
) -> str:
    """Schema + massa de dados COMPLETA (mode="full" — não o subconjunto do
    oráculo do smoke test: load/zipf.js amostra de toda a população real) +
    fixture de contexto-por-seletividade, antes de qualquer medição — reusa
    a mesma construção de env vars e passos de schema/carga do smoke test
    (Fase 4), sem os gates de correção/smoke, que não são responsabilidade
    desta fase. Bind-monta `fixtures_mount` para `contexts_by_tier.json`
    sobreviver e ser lido depois por cada `docker run` de
    `load/run_battery.py`."""
    env_flags = build_storage_env_flags(cell_id, storage, database_ip, service_ip, postgres_password)
    env_flags = [*env_flags, f"DATASET_BUCKET={dataset_bucket}"]
    steps = build_schema_and_fixture_steps(storage, mode="full")
    steps.append("python load/export_contexts_by_tier.py")
    inner = " && ".join(steps)

    docker_argv = [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "-v",
        f"{fixtures_mount}:/app/load/fixtures",
        "--entrypoint",
        "bash",
    ]
    for flag in env_flags:
        docker_argv += ["-e", flag]
    docker_argv += [tools_image, "-c", inner]
    return shlex.join(docker_argv)


def build_remote_battery_command(
    cell_id: str,
    target_url: str,
    phase: str,
    rate: int,
    tier: str,
    repetitions: int,
    tools_image: str,
    results_mount: str,
    fixtures_mount: str,
    timestamp: str,
) -> str:
    """Monta `docker run ... load/run_battery.py ...` como argv +
    shlex.join, mesmo padrão de segurança de
    cloud_smoke_test.py:build_remote_smoke_script (nunca concatenar strings
    com aspas manualmente). Bind-monta `results_mount` porque o container é
    `--rm` — sem isso, `results/` some quando ele sai — e `fixtures_mount`
    (somente leitura) para reusar o `contexts_by_tier.json` que o setup já
    gerou, sem reexportar em toda combinação de carga/seletividade.
    `--timestamp` fixo faz todas as combinações desta execução caírem no
    mesmo results/<cell>/<phase>/<timestamp>/, no mesmo diretório onde
    _write_saturation_json também escreve saturation.json."""
    battery_argv = [
        "load/run_battery.py",
        "--cells",
        cell_id,
        "--target-url",
        target_url,
        "--phase",
        phase,
        "--repetitions",
        str(repetitions),
        "--rate",
        str(rate),
        "--selectivity-tier",
        tier,
        "--timestamp",
        timestamp,
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
        "python",
        tools_image,
        *battery_argv,
    ]
    return shlex.join(docker_argv)


def build_remote_probe_command(
    cell_id: str,
    target_url: str,
    tier: str,
    rate: int,
    warmup: str,
    measure: str,
    tools_image: str,
    results_mount: str,
    fixtures_mount: str,
    remote_subdir: str,
    repetitions: int,
) -> str:
    """Sondagem de um único patamar da busca de saturação
    (load/saturation.py) — roda k6 (PROBE_MODE) `repetitions` vezes (1 na
    rampa curta, 5 na de confirmação) e consolida com
    analysis/probe_report.py na MESMA invocação de container: evita expor
    polars ao host (só o resultado de uma linha `PROBE_RESULT ...` volta
    via stdout do SSH, capturado por _parse_probe_result_line)."""
    steps: list[str] = []
    ndjson_paths: list[str] = []
    for rep in range(repetitions):
        json_out = f"/app/results/{remote_subdir}/rep{rep}/k6-raw.json"
        ndjson_paths.append(json_out)
        k6_argv = build_probe_k6_cmd(json_out, cell_id, target_url, rate, tier, warmup, measure)
        steps.append(shlex.join(str(a) for a in k6_argv))
    steps.append("python analysis/probe_report.py " + shlex.join(ndjson_paths))
    inner = " && ".join(steps)

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
        inner,
    ]
    return shlex.join(docker_argv)


def _parse_probe_result_line(stdout: str) -> bool:
    """Lê a linha `PROBE_RESULT violated_slo=True p99=... error_rate=... ` que
    analysis/probe_report.py imprime dentro do container remoto."""
    for line in stdout.splitlines():
        if line.startswith("PROBE_RESULT"):
            for token in line.split():
                if token.startswith("violated_slo="):
                    return token.split("=", 1)[1] == "True"
    raise RuntimeError(
        f"analysis/probe_report.py não imprimiu PROBE_RESULT na saída remota:\n{stdout}"
    )


def _generator_cpu_percent(project_id: str, loadgen_instance: str, start_time, end_time) -> float:
    """CPU da VM loadgen na janela de uma sondagem, via Cloud Monitoring —
    CONTEXTO.md: "válido só se CPU do gerador < 60%", checado a cada
    patamar da busca de saturação, não só ao final."""
    from analysis.resources import GCPMonitoringCollector

    collector = GCPMonitoringCollector(
        project_id=project_id,
        instance_by_component={"loadgen": loadgen_instance},
        memory_mb_by_component={},
        start_time=start_time,
        end_time=end_time,
    )
    samples = collector.collect()
    return samples[0].cpu_percent


def verify_otel_pipeline(
    project_id: str,
    instance_by_component: dict[str, str],
    memory_mb_by_component: dict[str, float],
    wait_seconds: int = 60,
) -> bool:
    """Confirma que o coletor OpenTelemetry das 3 VMs (infra/modules/
    {database,service,loadgen}/main.tf) está de fato exportando métricas
    para o Cloud Monitoring, antes de comprometer horas de VM numa triagem
    inteira — README.md/CONTEXTO.md sinalizam essa peça como a menos
    testada do projeto (Ops Agent oficial do Google não roda em COS, sem
    gerenciador de pacotes). Pensado para rodar só na primeira célula da
    triagem (--verify-otel): o módulo Terraform é idêntico nas 14, então
    um OK aqui vale para todas."""
    from analysis.resources import GCPMonitoringCollector

    print(f"Aguardando {wait_seconds}s para o coletor publicar a primeira amostra...")
    time.sleep(wait_seconds)

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(seconds=wait_seconds)
    collector = GCPMonitoringCollector(
        project_id, instance_by_component, memory_mb_by_component, start_time, end_time
    )
    try:
        samples = collector.collect()
    except Exception as exc:
        print(f"FALHOU: {exc}")
        print(
            "Diagnóstico: `gcloud compute ssh <instância> --zone=<zone> --project="
            f"{project_id} --tunnel-through-iap` e `docker logs tcc-otel-agent` em cada VM "
            "para ver o erro real. Métricas novas (workload.googleapis.com/*) às vezes levam "
            "alguns minutos a mais para ficar consultáveis na primeira vez — tente de novo "
            "antes de concluir que o coletor está quebrado."
        )
        return False

    for sample in samples:
        print(
            f"  {sample.component}: cpu={sample.cpu_percent:.1f}% "
            f"memory={sample.memory_mb:.0f}MB network={sample.network_mbps:.2f}Mbps"
        )
    print("OK: as 3 VMs estão exportando CPU/memória/rede para o Cloud Monitoring.")
    return True


def make_resource_collect_fn(
    project_id: str,
    instance_by_component: dict[str, str],
    memory_mb_by_component: dict[str, float],
    interval_seconds: int = RESOURCE_SAMPLE_INTERVAL_SECONDS,
) -> Callable[[], list]:
    """Fecha sobre o contexto de uma célula e devolve uma função sem
    argumentos que consulta uma janela curta e recente do Cloud Monitoring
    — usada por sample_resources_periodically. Consultar uma janela curta
    a cada tick (em vez de pedir a série histórica inteira de uma vez ao
    final) evita depender da granularidade exata que o Cloud Monitoring
    retém para cada métrica."""
    from analysis.resources import GCPMonitoringCollector

    def collect_fn() -> list:
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(seconds=interval_seconds)
        collector = GCPMonitoringCollector(
            project_id, instance_by_component, memory_mb_by_component, start_time, end_time
        )
        return collector.collect()

    return collect_fn


def sample_resources_periodically(
    collect_fn: Callable[[], list],
    stop_event: threading.Event,
    samples_out: list,
    interval_seconds: int = RESOURCE_SAMPLE_INTERVAL_SECONDS,
) -> None:
    """Chama collect_fn() a cada interval_seconds até stop_event ser
    sinalizado, acumulando em samples_out — roda numa thread separada,
    em paralelo ao sweep/rampa de confirmação (CONTEXTO.md: "amostrar a
    cada 5 segundos nas três VMs"). collect_fn isolado por injeção de
    dependência (make_resource_collect_fn) para este loop ser testável com
    um fake, sem precisar de Cloud Monitoring de verdade — uma falha
    isolada de coleta (rede, métrica ainda não disponível) não derruba o
    loop nem a medição em andamento."""
    while not stop_event.is_set():
        try:
            samples_out.extend(collect_fn())
        except Exception as exc:
            print(f"AVISO: falha ao amostrar recursos ({exc}) — pulando esta amostra.")
        stop_event.wait(interval_seconds)


def make_probe_fn(
    cell_id: str,
    target_url: str,
    tier: str,
    warmup: str,
    measure: str,
    loadgen_instance: str,
    zone: str,
    project_id: str,
    tools_image: str,
    results_mount: str,
    fixtures_mount: str,
    label: str,
    repetitions: int = 1,
) -> Callable[[int], ProbeResult]:
    """Fecha sobre o contexto de rede/infra de uma célula e devolve um
    probe_fn(rate) -> ProbeResult para load.saturation.run_saturation_search
    — cada chamada roda a sondagem remota via SSH, lê o veredito do SLO
    (analysis/probe_report.py, dentro do container) e consulta a CPU do
    gerador (Cloud Monitoring, no host) na mesma janela."""
    counter = itertools.count()

    def probe_fn(rate: int) -> ProbeResult:
        probe_id = f"{label}-{next(counter)}-{rate}"
        remote_subdir = f"_saturation/{cell_id}/{probe_id}"
        start_time = datetime.now(timezone.utc)

        remote_cmd = build_remote_probe_command(
            cell_id,
            target_url,
            tier,
            rate,
            warmup,
            measure,
            tools_image,
            results_mount,
            fixtures_mount,
            remote_subdir,
            repetitions,
        )
        result = gcloud_ssh(loadgen_instance, zone, project_id, remote_cmd)
        end_time = datetime.now(timezone.utc)

        violated = _parse_probe_result_line(result.stdout)
        generator_cpu = _generator_cpu_percent(project_id, loadgen_instance, start_time, end_time)

        return ProbeResult(rate=rate, violated_slo=violated, generator_cpu_percent=generator_cpu)

    return probe_fn


def _report_saturation(saturation, label: str = "") -> None:
    prefix = f"[{label}] " if label else ""
    if saturation.loadgen_bottleneck:
        print(
            f"{prefix}ERRO: o gerador de carga saturou (CPU >= {GENERATOR_CPU_THRESHOLD:.0f}%) "
            "durante a busca de saturação — a execução é inválida. Escale o gerador (tipo de "
            "máquina maior em infra/modules/loadgen) antes de repetir. Não use este resultado "
            "como vazão da célula."
        )
    elif saturation.censored:
        print(
            f"{prefix}célula não saturou nem no teto de {saturation.lower_bound:.0f} req/s — "
            "censurada nessa dimensão (CONTEXTO.md: tratada como empatada com outras censuradas "
            "e superior a qualquer não-censurada, nunca como o teto de verdade)."
        )
    else:
        print(f"{prefix}vazão de saturação aproximada: {saturation.approx_throughput:.0f} req/s.")


def _write_saturation_json(
    saturation, cell_id: str, phase: str, timestamp: str, filename: str = "saturation.json"
) -> None:
    out_dir = Path("results") / cell_id / phase / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "approx_throughput": saturation.approx_throughput,
        "censored": saturation.censored,
        "lower_bound": saturation.lower_bound,
        "loadgen_bottleneck": saturation.loadgen_bottleneck,
        "probes": [
            {
                "rate": p.rate,
                "violated_slo": p.violated_slo,
                "generator_cpu_percent": p.generator_cpu_percent,
            }
            for p in saturation.probes
        ],
    }
    (out_dir / filename).write_text(json.dumps(payload, indent=2))


def sync_results_from_loadgen(
    loadgen_instance: str, zone: str, project_id: str, remote_dir: str, local_dir: str
) -> None:
    _run(
        [
            "gcloud",
            "compute",
            "scp",
            "--recurse",
            "--tunnel-through-iap",
            f"--zone={zone}",
            f"--project={project_id}",
            f"{loadgen_instance}:{remote_dir}",
            local_dir,
        ]
    )


def upload_results_to_bucket(local_dir: Path, results_bucket: str, cell_id: str) -> None:
    _run(
        [
            "gcloud",
            "storage",
            "cp",
            "--recursive",
            str(local_dir),
            f"gs://{results_bucket}/{cell_id}/",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cell")
    parser.add_argument("project_id")
    parser.add_argument("region")
    parser.add_argument("zone")
    parser.add_argument("terraform_state_bucket")
    parser.add_argument("results_bucket")
    parser.add_argument("dataset_bucket")
    parser.add_argument("--phase", required=True, choices=["triagem", "confirmacao"])
    parser.add_argument("--repetitions", type=int, default=REPETITIONS)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--saturation-start",
        type=float,
        default=None,
        help="obrigatório com --phase confirmacao: valor aproximado (ou lower_bound, se "
        "censurada) que o report.json da triagem deu para esta célula — ponto de partida da "
        "rampa fina de confirmação.",
    )
    parser.add_argument(
        "--verify-otel",
        action="store_true",
        help="antes do sweep, confirma que o coletor OpenTelemetry das 3 VMs está exportando "
        "métricas de verdade para o Cloud Monitoring — recomendado só na primeira célula da "
        "triagem (o módulo Terraform é idêntico nas 14, um OK aqui vale para todas). Aborta "
        "(sem rodar o sweep) se a verificação falhar.",
    )
    parser.add_argument(
        "--keep-infra",
        action="store_true",
        help="não roda terraform destroy no final (para investigar uma falha)",
    )
    args = parser.parse_args(argv)

    if args.phase == "confirmacao" and args.saturation_start is None:
        print(
            "ERRO: --saturation-start é obrigatório com --phase confirmacao — leia o valor "
            "aproximado (ou lower_bound, se censurada) do report.json da triagem para esta "
            "célula.",
            file=sys.stderr,
        )
        return 1

    storage = storage_for_cell(args.cell)
    sweep = shuffled_sweep(build_sweep(args.phase), args.seed)
    print(f"sweep embaralhado (seed={args.seed}, fase={args.phase}): {sweep}")

    tools_image = os.environ.get("TOOLS_IMAGE")
    if not tools_image:
        print(
            "ERRO: defina a variável de ambiente TOOLS_IMAGE (mesma referência usada em "
            "terraform.tfvars) antes de rodar este script.",
            file=sys.stderr,
        )
        return 1

    print("Mintando token de acesso via impersonação da SA do Terraform (nunca fica em disco)...")
    os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = fetch_terraform_access_token(args.project_id)

    applied = False
    try:
        _confirm_billable(
            f"terraform apply da célula '{args.cell}' em {args.project_id}/{args.region} vai "
            f"criar VMs reais (banco + serviço + loadgen) e rodar {len(sweep)} combinação(ões) "
            f"de carga/seletividade x {args.repetitions} repetições, mais a busca de vazão de "
            "saturação — pode levar horas e cobra o tempo todo."
        )
        applied = True
        terraform(
            [
                "init",
                "-reconfigure",
                f"-backend-config=bucket={args.terraform_state_bucket}",
                f"-backend-config=prefix=cells/{args.cell}",
            ]
        )
        terraform(
            [
                "apply",
                f"-var=project_id={args.project_id}",
                f"-var=region={args.region}",
                f"-var=zone={args.zone}",
                f"-var=cell={args.cell}",
                f"-var=storage={storage}",
                f"-var=dataset_bucket={args.dataset_bucket}",
            ]
        )

        outputs = terraform_output_json()
        database_ip = outputs["database_internal_ip"]
        service_ip = outputs["service_internal_ip"]
        database_instance = f"tcc-{args.cell}-database"
        service_instance = f"tcc-{args.cell}-service"
        loadgen_instance = f"tcc-{args.cell}-loadgen"

        wait_for_container(database_instance, args.zone, args.project_id, "tcc-database")
        wait_for_container(service_instance, args.zone, args.project_id, "tcc-service")

        instance_by_component = {
            "database": database_instance,
            "service": service_instance,
            "loadgen": loadgen_instance,
        }

        if args.verify_otel:
            print("\n--- verificação do coletor OpenTelemetry/Ops Agent ---")
            if not verify_otel_pipeline(
                args.project_id, instance_by_component, DEFAULT_MEMORY_MB_BY_COMPONENT
            ):
                print(
                    "\nAbortando antes do sweep: corrija o coletor OpenTelemetry (README.md, "
                    "'Instrumentação de gargalo') antes de comprometer horas de VM numa "
                    "triagem inteira."
                )
                return 1

        postgres_password = None
        if storage == "postgres":
            secret_result = _run(
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
            postgres_password = secret_result.stdout.strip()

        setup_cmd = build_remote_setup_command(
            args.cell,
            storage,
            database_ip,
            service_ip,
            tools_image,
            postgres_password,
            FIXTURES_MOUNT,
            args.dataset_bucket,
        )
        gcloud_ssh(loadgen_instance, args.zone, args.project_id, setup_cmd)

        target_url = f"http://{service_ip}:8000/v1/recommendations"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        # Amostragem periódica de recursos (CONTEXTO.md: "amostrar a cada 5
        # segundos nas três VMs") — só durante a confirmação; a rampa curta
        # da triagem é exploratória e nunca é arquivada, o mesmo vale para o
        # uso de recursos dela.
        resource_samples: list = []
        stop_sampling = threading.Event()
        sampling_thread = None
        if args.phase == "confirmacao":
            collect_fn = make_resource_collect_fn(
                args.project_id, instance_by_component, DEFAULT_MEMORY_MB_BY_COMPONENT
            )
            sampling_thread = threading.Thread(
                target=sample_resources_periodically,
                args=(collect_fn, stop_sampling, resource_samples),
                daemon=True,
            )
            sampling_thread.start()

        for i, (rate, tier) in enumerate(sweep, start=1):
            print(f"\n--- combinação {i}/{len(sweep)}: rate={rate} tier={tier} ---")
            remote_cmd = build_remote_battery_command(
                args.cell,
                target_url,
                args.phase,
                rate,
                tier,
                args.repetitions,
                tools_image,
                RESULTS_MOUNT,
                FIXTURES_MOUNT,
                timestamp,
            )
            gcloud_ssh(loadgen_instance, args.zone, args.project_id, remote_cmd)

        if args.phase == "triagem":
            print("\n--- rampa curta de saturação (exploratória, CONTEXTO.md) ---")
            probe_fn = make_probe_fn(
                args.cell,
                target_url,
                TRIAGEM_TIER,
                SHORT_RAMP_WARMUP,
                SHORT_RAMP_MEASURE,
                loadgen_instance,
                args.zone,
                args.project_id,
                tools_image,
                RESULTS_MOUNT,
                FIXTURES_MOUNT,
                label="short",
                repetitions=1,
            )
            saturation = run_saturation_search(probe_fn, step_mode="doubling")
            _report_saturation(saturation)
            _write_saturation_json(saturation, args.cell, args.phase, timestamp)
        else:
            for tier in SELECTIVITY_TIERS:
                print(f"\n--- rampa de confirmação de saturação — seletividade {tier} ---")
                probe_fn = make_probe_fn(
                    args.cell,
                    target_url,
                    tier,
                    CONFIRMATION_WARMUP,
                    CONFIRMATION_MEASURE,
                    loadgen_instance,
                    args.zone,
                    args.project_id,
                    tools_image,
                    RESULTS_MOUNT,
                    FIXTURES_MOUNT,
                    label=f"confirm-{tier}",
                    repetitions=CONFIRMATION_REPETITIONS,
                )
                saturation = run_saturation_search(
                    probe_fn, start_rate=int(args.saturation_start), step_mode="fine"
                )
                _report_saturation(saturation, label=tier)
                _write_saturation_json(
                    saturation, args.cell, args.phase, timestamp, filename=f"saturation_{tier}.json"
                )

        if sampling_thread is not None:
            stop_sampling.set()
            sampling_thread.join(timeout=RESOURCE_SAMPLE_INTERVAL_SECONDS + 10)
            if resource_samples:
                from analysis.resources import classify_bottleneck, write_resources_csv

                resources_path = Path("results") / args.cell / args.phase / timestamp / "resources.csv"
                write_resources_csv(resource_samples, resources_path)
                print(f"\n{resources_path}: {len(resource_samples)} amostras de recursos escritas.")
                try:
                    bottleneck = classify_bottleneck(
                        resource_samples, memory_ceiling_mb=DEFAULT_MEMORY_MB_BY_COMPONENT
                    )
                    print(f"Gargalo dominante ao longo da confirmação: {bottleneck}.")
                except ValueError:
                    pass
            else:
                print("\nAVISO: nenhuma amostra de recursos coletada — resources.csv não foi escrito.")

        if args.phase == "confirmacao":
            # Diferente da rampa curta (exploratória, nunca arquivada), a de
            # confirmação exige "saída com distribuição completa" — traz de
            # volta as sondagens brutas para results/_saturation/, fora do
            # namespace results/<cell>/<phase>/ que analysis/report.py varre
            # (nunca entra por engano numa tabela de medição comum).
            local_saturation_dir = Path("results") / "_saturation" / args.cell
            sync_results_from_loadgen(
                loadgen_instance,
                args.zone,
                args.project_id,
                f"{RESULTS_MOUNT}/_saturation/{args.cell}",
                str(local_saturation_dir),
            )
            upload_results_to_bucket(local_saturation_dir, args.results_bucket, f"_saturation/{args.cell}")

        local_results_dir = Path("results") / args.cell
        sync_results_from_loadgen(
            loadgen_instance,
            args.zone,
            args.project_id,
            f"{RESULTS_MOUNT}/{args.cell}",
            str(local_results_dir),
        )
        upload_results_to_bucket(local_results_dir, args.results_bucket, args.cell)

        print(f"\n[{loadgen_instance}] recursos:")
        print(resource_snapshot(loadgen_instance, args.zone, args.project_id))

    finally:
        if not applied:
            print("\nNenhum recurso foi criado (apply nunca rodou) — nada para destruir.")
        elif args.keep_infra:
            print(
                "\n--keep-infra: infraestrutura NÃO foi derrubada. "
                "Lembre de destruir manualmente depois."
            )
        else:
            _confirm_billable(
                f"terraform destroy da célula '{args.cell}' — é isso que PARA a cobrança."
            )
            terraform(
                [
                    "destroy",
                    f"-var=project_id={args.project_id}",
                    f"-var=region={args.region}",
                    f"-var=zone={args.zone}",
                    f"-var=cell={args.cell}",
                    f"-var=storage={storage}",
                    f"-var=dataset_bucket={args.dataset_bucket}",
                ]
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
