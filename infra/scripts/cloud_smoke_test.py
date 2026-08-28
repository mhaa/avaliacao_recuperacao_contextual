#!/usr/bin/env python3
"""Smoke test em nuvem — sobe uma célula de verdade no GCP, roda uma carga
leve, confirma que funciona, e derruba tudo. Objetivo: pegar problemas antes
da bateria de medição real (cara, demorada, "não pode ser re-executada" sem
custo) — ver README.md, "Fase 5" (ou o plano de implementação em
C:\\Users\\marce\\.claude\\plans, se ainda não tiver virado seção do README).

Roda no HOST (não dentro do container `tools`, que não tem o gcloud CLI —
só terraform+k6+Python, ver docker/Dockerfile.tools). Terraform roda via
docker-compose.gcp.yml; tudo mais roda dentro de `docker run --rm
--network host <tools_image> ...` executado remotamente via `gcloud compute
ssh <loadgen> --tunnel-through-iap` — as VMs de banco/serviço/loadgen são
Container-Optimized OS (só Docker, sem Python/psql/bash com ferramentas), e
a sub-rede é 100% privada (sem IPs públicos, infra/modules/network/main.tf).

Uso:
    python infra/scripts/cloud_smoke_test.py <cell> <project-id> <region> <zone> \\
        <terraform-state-bucket> [--keep-infra]

Cada comando faturável (terraform apply/destroy) é anunciado explicitamente
antes de rodar e pede confirmação — mesmo com o plano já aprovado (regra
permanente pedida pelo usuário).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

REPO_ROOT_RELATIVE_TF_DIR = "infra/envs/experiment"
STORAGES_WITH_SCHEMA_APPLY = {
    "postgres": "schemas/postgres/apply_schema.py",
    "scylla": "schemas/scylla/apply_schema.py",
    "opensearch": "schemas/opensearch/create_index.py",
    # valkey: sem schema, nada a aplicar.
}


def storage_for_cell(cell_id: str) -> str:
    # cells/<id>.yaml segue sempre o padrão e<1-4>-<storage> — mesma
    # convenção de nome já usada em toda a suíte de testes
    # (tests/acceptance/test_harness_all_cells.py:VIABLE_CELL_IDS), não uma
    # segunda fonte de verdade nova.
    return cell_id.split("-", 1)[1]


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kwargs)


def _confirm_billable(message: str) -> None:
    print()
    print("=" * 70)
    print(f"FATURÁVEL: {message}")
    print("=" * 70)
    answer = input("Digite 'sim' para continuar, qualquer outra coisa para abortar: ")
    if answer.strip().lower() != "sim":
        print("Abortado pelo usuário.")
        sys.exit(1)


def terraform(args: list[str]) -> None:
    cmd = [
        "docker",
        "compose",
        "-f",
        "docker-compose.yml",
        "-f",
        "docker-compose.gcp.yml",
        "run",
        "--rm",
        "--entrypoint",
        "terraform",
        "tools",
        f"-chdir={REPO_ROOT_RELATIVE_TF_DIR}",
        *args,
    ]
    _run(cmd)


def terraform_output_json() -> dict:
    cmd = [
        "docker",
        "compose",
        "-f",
        "docker-compose.yml",
        "-f",
        "docker-compose.gcp.yml",
        "run",
        "--rm",
        "--entrypoint",
        "terraform",
        "tools",
        f"-chdir={REPO_ROOT_RELATIVE_TF_DIR}",
        "output",
        "-json",
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    parsed = json.loads(result.stdout)
    return {k: v["value"] for k, v in parsed.items()}


def gcloud_ssh(
    instance: str, zone: str, project_id: str, remote_command: str
) -> subprocess.CompletedProcess:
    cmd = [
        "gcloud",
        "compute",
        "ssh",
        instance,
        f"--zone={zone}",
        f"--project={project_id}",
        "--tunnel-through-iap",
        f"--command={remote_command}",
    ]
    return _run(cmd, capture_output=True, text=True)


def wait_for_container(
    instance: str, zone: str, project_id: str, container_name: str, timeout_s: int = 300
) -> None:
    print(f"Aguardando container '{container_name}' em {instance}...")
    deadline = time.monotonic() + timeout_s
    check_cmd = f"docker ps --filter name={container_name} --filter status=running -q"
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                [
                    "gcloud",
                    "compute",
                    "ssh",
                    instance,
                    f"--zone={zone}",
                    f"--project={project_id}",
                    "--tunnel-through-iap",
                    f"--command={check_cmd}",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.stdout.strip():
                print(f"  {container_name} no ar.")
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(10)
    raise TimeoutError(f"container '{container_name}' não subiu em {timeout_s}s")


def build_remote_smoke_script(
    cell_id: str,
    storage: str,
    database_ip: str,
    service_ip: str,
    tools_image: str,
    postgres_password: str | None,
) -> str:
    env_flags = [f"-e CELL={cell_id}", f"-e STORAGE_HOST={database_ip}"]
    if storage == "postgres":
        env_flags += [
            "-e POSTGRES_USER=tcc",
            f"-e POSTGRES_PASSWORD={postgres_password}",
            "-e POSTGRES_DB=recsys",
            f"-e TEST_POSTGRES_DSN=postgresql://tcc:{postgres_password}@{database_ip}:5432/recsys",
        ]
    elif storage == "scylla":
        env_flags += [f"-e TEST_SCYLLA_HOSTS={database_ip}"]
    elif storage == "opensearch":
        env_flags += [f"-e TEST_OPENSEARCH_HOST=http://{database_ip}:9200"]
    # valkey: sem credencial nem TEST_* — build_storage() usa STORAGE_HOST direto.

    schema_step = STORAGES_WITH_SCHEMA_APPLY.get(storage)
    steps = []
    if schema_step:
        steps.append(f"python {schema_step}")
    fixture_loader = f"schemas/{storage}/load_oracle_fixture.py"
    steps.append(f"python {fixture_loader}")
    steps.append(f"python -m harness.verify_cli --cell {cell_id}")
    steps.append("python load/export_contexts_by_tier.py")
    steps.append(
        "SMOKE_MODE=true "
        f"TARGET_URL=http://{service_ip}:8000/v1/recommendations "
        f"CELL={cell_id} "
        "k6 run load/scenarios.js --out json=/tmp/smoke-raw.json"
    )
    steps.append(
        "python -c \""
        "from pathlib import Path; "
        "from analysis.collect import parse_k6_ndjson; "
        "df = parse_k6_ndjson(Path('/tmp/smoke-raw.json'), scenarios=frozenset({'smoke'})); "
        "errors = df.filter(df['status'] != 200); "
        "print(f'smoke: {len(df)} requisicoes, {len(errors)} com erro'); "
        "print(df['latency_ms'].describe()) if len(df) else print('smoke: 0 requisicoes (nada foi parseado — ver k6-raw.json)')"
        "\""
    )
    inner = " && ".join(steps)
    env_str = " ".join(env_flags)
    return f'docker run --rm --network host {env_str} {tools_image} bash -c "{inner}"'


def resource_snapshot(instance: str, zone: str, project_id: str) -> str:
    result = gcloud_ssh(instance, zone, project_id, "docker stats --no-stream")
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cell")
    parser.add_argument("project_id")
    parser.add_argument("region")
    parser.add_argument("zone")
    parser.add_argument("terraform_state_bucket")
    parser.add_argument(
        "--keep-infra",
        action="store_true",
        help="não roda terraform destroy no final (para investigar uma falha)",
    )
    args = parser.parse_args(argv)

    storage = storage_for_cell(args.cell)
    phase_times: list[tuple[str, float]] = []

    def _phase(name: str) -> None:
        phase_times.append((name, time.monotonic()))
        print(f"\n--- {name} ---")

    try:
        _confirm_billable(
            f"terraform apply da célula '{args.cell}' em {args.project_id}/{args.region} "
            "vai criar VMs reais (banco + serviço + loadgen) e começar a cobrar."
        )
        _phase("terraform init")
        terraform(
            [
                "init",
                f"-backend-config=bucket={args.terraform_state_bucket}",
                f"-backend-config=prefix=cells/{args.cell}",
            ]
        )
        _phase("terraform apply")
        terraform(
            [
                "apply",
                f"-var=project_id={args.project_id}",
                f"-var=region={args.region}",
                f"-var=zone={args.zone}",
                f"-var=cell={args.cell}",
                f"-var=storage={storage}",
            ]
        )

        _phase("ler outputs")
        outputs = terraform_output_json()
        database_ip = outputs["database_internal_ip"]
        service_ip = outputs["service_internal_ip"]
        database_instance = f"tcc-{args.cell}-database"
        service_instance = f"tcc-{args.cell}-service"
        loadgen_instance = f"tcc-{args.cell}-loadgen"

        _phase("esperar VMs")
        wait_for_container(database_instance, args.zone, args.project_id, "tcc-database")
        wait_for_container(service_instance, args.zone, args.project_id, "tcc-service")

        postgres_password = None
        if storage == "postgres":
            _phase("buscar senha do Postgres no Secret Manager")
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

        _phase("schema + fixture + verify + smoke (via loadgen)")
        # tools_image não é um output do Terraform hoje — passado por fora
        # (mesma referência que foi usada em terraform.tfvars pra essa
        # aplicação). Se preferir, exporte TOOLS_IMAGE no shell antes de
        # rodar este script; aqui só um fallback explícito de erro.
        import os

        tools_image = os.environ.get("TOOLS_IMAGE")
        if not tools_image:
            raise RuntimeError(
                "defina a variável de ambiente TOOLS_IMAGE (mesma referência usada em "
                "terraform.tfvars) antes de rodar este script"
            )
        remote_cmd = build_remote_smoke_script(
            args.cell, storage, database_ip, service_ip, tools_image, postgres_password
        )
        result = gcloud_ssh(loadgen_instance, args.zone, args.project_id, remote_cmd)
        print(result.stdout)
        if result.returncode != 0:
            print(result.stderr, file=sys.stderr)
            raise RuntimeError("smoke test remoto falhou — ver saída acima")

        _phase("sanidade grosseira de recursos")
        for instance in (database_instance, service_instance, loadgen_instance):
            print(f"\n[{instance}]")
            print(resource_snapshot(instance, args.zone, args.project_id))

    finally:
        phase_times.append(("fim", time.monotonic()))
        print("\n--- tempo por fase ---")
        for (name, start), (_, end) in zip(phase_times, phase_times[1:]):
            print(f"{name}: {end - start:.1f}s")
        print(
            "\nTipos de máquina (ver preço público do GCP para estimar custo): "
            "database n2-standard-8, service n2-standard-4, loadgen n2-standard-8 "
            "(padrões de infra/modules/*, salvo override em terraform.tfvars)."
        )

        if args.keep_infra:
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
                ]
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
