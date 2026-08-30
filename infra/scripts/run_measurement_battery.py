#!/usr/bin/env python3
"""Bateria de medição real em nuvem — README.md, "Fase 5". Contraparte de
infra/scripts/cloud_smoke_test.py (Fase 4): em vez de um smoke curto, roda
load/run_battery.py de verdade contra a célula, varrendo carga ×
seletividade conforme CONTEXTO.md ("Protocolo de medição" /
"Delineamento em duas etapas"), e traz os `results/` de volta.

Roda no HOST (não dentro do container `tools`), pelo mesmo motivo de
cloud_smoke_test.py: a imagem `tools` não tem o `gcloud` CLI, só
terraform+k6+Python. Terraform roda via docker-compose.gcp.yml; a bateria de
verdade roda dentro de `docker run --rm --network host <tools_image> ...`
executado remotamente via `gcloud compute ssh <loadgen> --tunnel-through-iap`
— os IPs vêm de `terraform output -json`, a VM de serviço não tem IP
público.

Uso (via `-m`, não como caminho de arquivo direto — este módulo importa
infra.scripts.cloud_smoke_test por nome absoluto, e `infra/` só existe no
container por bind mount, nunca copiado pela imagem; `-m` garante que /app
entra no sys.path, `python infra/scripts/run_measurement_battery.py` não):
    python -m infra.scripts.run_measurement_battery <cell> <project-id> <region> <zone> \\
        <terraform-state-bucket> <results-bucket> <dataset-bucket> --phase triagem \\
        [--repetitions 5] [--seed 42] [--ramp] [--keep-infra]

Cada comando faturável (terraform apply/destroy) é anunciado explicitamente
antes de rodar e pede confirmação — mesma regra permanente de
cloud_smoke_test.py, pedida pelo usuário.
"""

from __future__ import annotations

import argparse
import os
import random
import shlex
import sys
from pathlib import Path

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
    ramp: bool,
    tools_image: str,
    results_mount: str,
    fixtures_mount: str,
) -> str:
    """Monta `docker run ... load/run_battery.py ...` como argv +
    shlex.join, mesmo padrão de segurança de
    cloud_smoke_test.py:build_remote_smoke_script (nunca concatenar strings
    com aspas manualmente). Bind-monta `results_mount` porque o container é
    `--rm` — sem isso, `results/` some quando ele sai — e `fixtures_mount`
    (somente leitura) para reusar o `contexts_by_tier.json` que o setup já
    gerou, sem reexportar em toda combinação de carga/seletividade."""
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
    ]
    if ramp:
        battery_argv.append("--ramp")

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
        "--ramp",
        action="store_true",
        help="depois do sweep, roda também a rampa até violar o SLO (seletividade 'medium', "
        "rate ignorado pelo executor ramping-arrival-rate — ver load/scenarios.js)",
    )
    parser.add_argument(
        "--keep-infra",
        action="store_true",
        help="não roda terraform destroy no final (para investigar uma falha)",
    )
    args = parser.parse_args(argv)

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
            f"de carga/seletividade x {args.repetitions} repetições — pode levar horas e cobra "
            "o tempo todo."
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
        for i, (rate, tier) in enumerate(sweep, start=1):
            print(f"\n--- combinação {i}/{len(sweep)}: rate={rate} tier={tier} ---")
            remote_cmd = build_remote_battery_command(
                args.cell,
                target_url,
                args.phase,
                rate,
                tier,
                args.repetitions,
                False,
                tools_image,
                RESULTS_MOUNT,
                FIXTURES_MOUNT,
            )
            gcloud_ssh(loadgen_instance, args.zone, args.project_id, remote_cmd)

        if args.ramp:
            print("\n--- rampa até violar o SLO ---")
            remote_cmd = build_remote_battery_command(
                args.cell,
                target_url,
                args.phase,
                RATES[0],
                TRIAGEM_TIER,
                1,
                True,
                tools_image,
                RESULTS_MOUNT,
                FIXTURES_MOUNT,
            )
            gcloud_ssh(loadgen_instance, args.zone, args.project_id, remote_cmd)

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
