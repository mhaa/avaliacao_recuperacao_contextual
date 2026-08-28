#!/usr/bin/env python3
"""Smoke test em nuvem — sobe uma célula de verdade no GCP, roda uma carga
leve, confirma que funciona, e derruba tudo. Objetivo: pegar problemas antes
da bateria de medição real (cara, demorada, "não pode ser re-executada" sem
custo) — ver README.md, "Fase 4".

Roda no HOST (não dentro do container `tools`, que não tem o gcloud CLI —
só terraform+k6+Python, ver docker/Dockerfile.tools). Terraform roda via
docker-compose.gcp.yml; tudo mais roda dentro de `docker run --rm
--network host <tools_image> ...` executado remotamente via `gcloud compute
ssh <loadgen> --tunnel-through-iap` — as VMs de banco/serviço/loadgen são
Container-Optimized OS (só Docker, sem Python/psql/bash com ferramentas), e
a sub-rede é 100% privada (sem IPs públicos, infra/modules/network/main.tf).

Credencial do Terraform: buscada do Secret Manager (`tcc-terraform-key`,
criado por infra/scripts/create_terraform_service_account.sh) para um
arquivo temporário no início da execução, e apagada no final — nunca
persiste em disco além da duração deste processo. Usa a sessão pessoal do
operador (`gcloud auth login`) pra ler o secret, não a própria service
account do Terraform (seria circular).

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
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT_RELATIVE_TF_DIR = "infra/envs/experiment"
TERRAFORM_KEY_SECRET = "tcc-terraform-key"
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


def fetch_terraform_key(project_id: str) -> str:
    """Busca a chave da service account do Terraform no Secret Manager para
    um arquivo temporário (permissão 0600 por padrão do tempfile — só o
    dono lê). O chamador é responsável por apagar o caminho retornado
    (ver `main()`, bloco try/finally externo) assim que não precisar mais
    dele nesta execução."""
    result = _run(
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "latest",
            f"--secret={TERRAFORM_KEY_SECRET}",
            f"--project={project_id}",
        ],
        capture_output=True,
        text=True,
    )
    fd, path = tempfile.mkstemp(suffix=".json", prefix="tcc-terraform-key-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(result.stdout)
    except BaseException:
        os.unlink(path)
        raise
    return path


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
    print(f"+ {' '.join(cmd)}")
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
    """Monta o comando remoto como uma lista de argv (docker run ...) e usa
    shlex.join para virar uma única string shell-segura — nunca
    concatenação manual de strings com aspas embutidas (isso já causou um
    bug real: um `python -c "..."` com aspas duplas internas quebrava a
    sintaxe do `bash -c "..."` que o envolvia quando entregue via
    `gcloud compute ssh --command=...`, ver analysis/smoke_report.py)."""
    env_flags: list[str] = [f"CELL={cell_id}", f"STORAGE_HOST={database_ip}"]
    if storage == "postgres":
        env_flags += [
            "POSTGRES_USER=tcc",
            f"POSTGRES_PASSWORD={postgres_password}",
            "POSTGRES_DB=recsys",
            f"TEST_POSTGRES_DSN=postgresql://tcc:{postgres_password}@{database_ip}:5432/recsys",
        ]
    elif storage == "valkey":
        env_flags += [f"TEST_VALKEY_URL=redis://{database_ip}:6379/0"]
    elif storage == "scylla":
        env_flags += [f"TEST_SCYLLA_HOSTS={database_ip}"]
    elif storage == "opensearch":
        env_flags += [f"TEST_OPENSEARCH_HOST=http://{database_ip}:9200"]

    schema_step = STORAGES_WITH_SCHEMA_APPLY.get(storage)
    steps = []
    if schema_step:
        steps.append(f"python {schema_step}")
    steps.append(f"python schemas/{storage}/load_oracle_fixture.py")
    steps.append(f"python -m harness.verify_cli --cell {cell_id}")
    steps.append("python load/export_contexts_by_tier.py")
    steps.append(
        f"SMOKE_MODE=true TARGET_URL=http://{service_ip}:8000/v1/recommendations "
        f"CELL={cell_id} k6 run load/scenarios.js --out json=/tmp/smoke-raw.json"
    )
    steps.append("python analysis/smoke_report.py /tmp/smoke-raw.json")
    inner = " && ".join(steps)

    docker_argv = ["docker", "run", "--rm", "--network", "host"]
    for flag in env_flags:
        docker_argv += ["-e", flag]
    docker_argv += [tools_image, "bash", "-c", inner]
    return shlex.join(docker_argv)


def resource_snapshot(instance: str, zone: str, project_id: str) -> str:
    try:
        result = gcloud_ssh(instance, zone, project_id, "docker stats --no-stream")
        return result.stdout
    except subprocess.CalledProcessError as e:
        # Sanidade "grosseira" (best-effort) — uma falha aqui não deve
        # abortar o smoke test nem impedir o destroy no finally.
        return f"(falha ao coletar docker stats de {instance}: {e.stderr or e})"


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
    applied = False

    def _phase(name: str) -> None:
        phase_times.append((name, time.monotonic()))
        print(f"\n--- {name} ---")

    tools_image = os.environ.get("TOOLS_IMAGE")
    if not tools_image:
        print(
            "ERRO: defina a variável de ambiente TOOLS_IMAGE (mesma referência usada em "
            "terraform.tfvars) antes de rodar este script.",
            file=sys.stderr,
        )
        return 1

    print("Buscando credencial do Terraform no Secret Manager (nunca fica em disco)...")
    key_path = fetch_terraform_key(args.project_id)
    os.environ["GCP_TERRAFORM_KEY_PATH"] = key_path
    try:
        try:
            _confirm_billable(
                f"terraform apply da célula '{args.cell}' em {args.project_id}/{args.region} "
                "vai criar VMs reais (banco + serviço + loadgen) e começar a cobrar."
            )
            applied = True  # a partir daqui, apply pode ter criado recursos reais
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
            remote_cmd = build_remote_smoke_script(
                args.cell, storage, database_ip, service_ip, tools_image, postgres_password
            )
            try:
                result = gcloud_ssh(loadgen_instance, args.zone, args.project_id, remote_cmd)
            except subprocess.CalledProcessError as e:
                print(e.stdout)
                print(e.stderr, file=sys.stderr)
                raise RuntimeError("smoke test remoto falhou — ver saída acima") from e
            print(result.stdout)

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
                    ]
                )
    finally:
        Path(key_path).unlink(missing_ok=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
