#!/usr/bin/env python3
"""Mede o armazenamento REAL ocupado por tabela/padrão-de-chave/índice de
UMA tecnologia de banco, contra a base já carregada — docs/DESIGN.md,
"Custo de armazenamento — a componente que faltava na dimensão `custo`".
Uma execução por tecnologia, não por célula: armazenamento não varia com
carga/taxa de requisição (`analysis/storage_size.py` faz a medição de
verdade; este script só orquestra a infra).

Postgres/Scylla/OpenSearch restauram do snapshot de dataset já existente
(infra/scripts/seed_dataset_snapshots.py) quando ele existir — rápido, sem
recarregar ~500M linhas. Se ainda não houver snapshot para a tecnologia,
roda a carga completa antes de medir (mesmo caminho de
seed_dataset_snapshots.py, sem tirar snapshot ao final — este script só
mede). Valkey nunca tem snapshot (sem disco persistente) — sempre paga uma
carga completa, mesmo custo que já acontece hoje em toda célula real dessa
tecnologia.

Roda no HOST, mesmo motivo de seed_dataset_snapshots.py/cloud_smoke_test.py:
usa infra/envs/seed/ (réplica enxuta de infra/envs/experiment — só network +
database + loadgen) e SSH via IAP na VM de loadgen.

Uso (via -m, mesmo motivo dos outros scripts de infra/scripts/ — este
módulo importa infra.scripts.* por nome absoluto):
    python -m infra.scripts.measure_storage_size <postgres|valkey|scylla|opensearch> \\
        <project-id> <region> <zone> <terraform-state-bucket> <dataset-bucket> \\
        <results-bucket> [--keep-infra]

Grava o resultado em results/storage/<storage>.json — lido depois por
analysis/report.py para compor `custo_usd_hora` de cada célula daquela
tecnologia.

Cada comando faturável (terraform apply/destroy) é anunciado explicitamente
antes de rodar e pede confirmação — mesma regra permanente de
cloud_smoke_test.py, pedida pelo usuário.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from infra.scripts.cloud_smoke_test import (
    _confirm_billable,
    _run,
    build_storage_env_flags,
    fetch_terraform_access_token,
    gcloud_ssh,
    terraform,
    terraform_output_json,
    wait_for_container,
)
from infra.scripts.run_measurement_battery import build_remote_setup_command, snapshot_exists
from infra.scripts.seed_dataset_snapshots import snapshot_name_for

TF_DIR = "infra/envs/seed"
VALID_STORAGES = ("postgres", "valkey", "scylla", "opensearch")
# Mesmo diretório de bind mount usado por seed_dataset_snapshots.py — só
# precisa sobreviver ao `docker run --rm` do setup remoto, nunca é lido
# depois.
FIXTURES_MOUNT = "/home/tcc/load-fixtures"
STORAGE_OUT_DIR = Path("results/storage")


def build_remote_measure_command(
    cell_id: str,
    storage: str,
    database_ip: str,
    postgres_password: str | None,
    tools_image: str,
) -> str:
    """`docker run` remoto que roda `python -m analysis.storage_size
    <storage>` — as variáveis de conexão (TEST_POSTGRES_DSN etc.) chegam
    via `-e`, mesma convenção de `build_remote_setup_command`
    (`build_storage_env_flags`, único lugar que sabe montar essas
    variáveis por storage). `service_ip=database_ip`: este root não tem
    módulo `service`, e `TEST_SERVICE_URL` nunca é lida por
    `analysis/storage_size.py` — mesmo valor inofensivo já usado por
    `seed_dataset_snapshots.py`.

    Envolvido num retry (mesmo idioma já usado nos startup-scripts de
    infra/modules/database e /loadgen: `for i in 1..5; do CMD && break ||
    sleep 10; done`) — `wait_for_container` só confirma que o PROCESSO do
    banco subiu, não que ele já aceita conexões/consultas. Confirmado ao
    vivo restaurando de snapshot: Postgres respondeu "the database system
    is not yet accepting connections" no primeiro SSH, poucos segundos
    depois de o container aparecer no `docker ps`."""
    env_flags = build_storage_env_flags(cell_id, storage, database_ip, database_ip, postgres_password)
    docker_argv = ["docker", "run", "--rm", "--network", "host"]
    for flag in env_flags:
        docker_argv += ["-e", flag]
    docker_argv += ["--entrypoint", "python", tools_image, "-m", "analysis.storage_size", storage]
    measure_cmd = shlex.join(docker_argv)
    # Sem `bash -c` extra aqui: gcloud_ssh() já embrulha o `remote_command`
    # inteiro em base64 + `bash -c 'echo ... | base64 -d | bash'` — este
    # script já chega pronto pra rodar como o corpo desse bash remoto,
    # mesmo padrão de build_remote_setup_command (que também devolve um
    # script cru, nunca um `bash -c` aninhado).
    return (
        f"for i in 1 2 3 4 5; do {measure_cmd} && break || "
        '{ echo "tentativa $i falhou, esperando o banco terminar de subir..."; sleep 15; }; done'
    )


def resolve_snapshot_strategy(
    storage: str, candidate_snapshot_name: str, snapshot_found: bool
) -> tuple[str, bool]:
    """Decide se a medição restaura de um snapshot existente ou paga uma
    carga completa antes — separada de `snapshot_exists()` (chamada real à
    GCP) pra ser testável sem mock de API. Valkey nunca tem snapshot (sem
    disco persistente, ver README.md) — sempre carga completa,
    independente de `snapshot_found`. Postgres/Scylla/OpenSearch restauram
    quando o snapshot já existir; senão, também pagam a carga completa
    (mesmo custo de semear pela primeira vez)."""
    if storage != "valkey" and snapshot_found:
        return candidate_snapshot_name, True
    return "", False


def _parse_storage_result(stdout: str, stderr: str = "") -> dict:
    """Lê a linha `STORAGE_RESULT <json>` que analysis/storage_size.py
    imprime dentro do container remoto — mesmo padrão de
    infra/scripts/run_measurement_battery.py:_parse_probe_result (um
    processo só decide, uma linha só de saída). Inclui `stderr` na mensagem
    de erro — confirmado ao vivo que o caso real (imagem `tools:latest` no
    Artifact Registry desatualizada em relação ao código local, sem
    `main()`/CLI ainda) sai com código 0 e stdout VAZIO (`python -m` sobre
    um módulo sem bloco `__main__` só importa e sai) — sem stderr também
    exposto, o diagnóstico fica cego."""
    prefix = "STORAGE_RESULT "
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return json.loads(line[len(prefix) :])
    raise RuntimeError(
        "analysis/storage_size.py não imprimiu STORAGE_RESULT na saída remota "
        f"(stdout vazio costuma ser imagem tools:latest desatualizada no Artifact Registry, "
        "não só um erro remoto — confirme com infra/scripts/build_and_push_images.sh antes de "
        f"insistir):\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("storage", choices=VALID_STORAGES)
    parser.add_argument("project_id")
    parser.add_argument("region")
    parser.add_argument("zone")
    parser.add_argument("terraform_state_bucket")
    parser.add_argument("dataset_bucket")
    parser.add_argument(
        "results_bucket",
        help="output do bootstrap (results_bucket) — esta medição não sobe resultado nenhum, "
        "mas module.loadgen exige o valor pra criar a IAM binding de escrita, mesmo motivo de "
        "dataset_bucket.",
    )
    parser.add_argument(
        "--keep-infra",
        action="store_true",
        help="não roda terraform destroy no final (para investigar uma falha na medição)",
    )
    args = parser.parse_args(argv)

    tools_image = os.environ.get("TOOLS_IMAGE")
    if not tools_image:
        print(
            "ERRO: defina a variável de ambiente TOOLS_IMAGE (mesma referência usada em "
            "terraform.tfvars) antes de rodar este script.",
            file=sys.stderr,
        )
        return 1

    seed_cell = f"seed-{args.storage}"

    print("Mintando token de acesso via impersonação da SA do Terraform (nunca fica em disco)...")
    os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = fetch_terraform_access_token(args.project_id)

    candidate = snapshot_name_for(args.storage) if args.storage != "valkey" else ""
    snapshot_found = bool(candidate) and snapshot_exists(args.project_id, candidate)
    data_disk_snapshot, skip_dataset_load = resolve_snapshot_strategy(
        args.storage, candidate, snapshot_found
    )
    if skip_dataset_load:
        print(
            f"snapshot '{data_disk_snapshot}' encontrado — disco será restaurado dele, medindo "
            "sem recarregar o dataset."
        )
    elif args.storage != "valkey":
        print(
            f"snapshot '{candidate}' não encontrado — a carga completa vai rodar antes de medir "
            "(mesmo custo de semear pela primeira vez)."
        )

    applied = False
    try:
        _confirm_billable(
            f"terraform apply do root de seed (storage={args.storage}) em "
            f"{args.project_id}/{args.region} vai criar VMs reais (banco + loadgen) "
            + (
                "restaurando do snapshot de dataset já existente"
                if skip_dataset_load
                else "e carregar o dataset completo"
            )
            + " só para medir armazenamento real — pode levar tempo e cobra o tempo todo."
        )
        applied = True
        terraform(
            [
                "init",
                "-reconfigure",
                f"-backend-config=bucket={args.terraform_state_bucket}",
                f"-backend-config=prefix=seed/{args.storage}",
            ],
            tf_dir=TF_DIR,
        )
        terraform(
            [
                "apply",
                "-auto-approve",
                f"-var=project_id={args.project_id}",
                f"-var=region={args.region}",
                f"-var=zone={args.zone}",
                f"-var=storage={args.storage}",
                f"-var=tools_image={tools_image}",
                f"-var=dataset_bucket={args.dataset_bucket}",
                f"-var=results_bucket={args.results_bucket}",
                f"-var=data_disk_snapshot={data_disk_snapshot}",
            ],
            tf_dir=TF_DIR,
        )

        outputs = terraform_output_json(tf_dir=TF_DIR)
        database_ip = outputs["database_internal_ip"]
        database_instance = f"tcc-{seed_cell}-database"
        loadgen_instance = f"tcc-{seed_cell}-loadgen"

        wait_for_container(database_instance, args.zone, args.project_id, "tcc-database")

        postgres_password = None
        if args.storage == "postgres":
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

        if not skip_dataset_load:
            setup_cmd = build_remote_setup_command(
                seed_cell,
                args.storage,
                database_ip,
                database_ip,
                tools_image,
                postgres_password,
                FIXTURES_MOUNT,
                args.dataset_bucket,
            )
            gcloud_ssh(loadgen_instance, args.zone, args.project_id, setup_cmd)
            print(f"\nCarga completa concluída em {database_instance}. Medindo armazenamento...")
        else:
            print(f"\nMedindo armazenamento em {database_instance}...")

        measure_cmd = build_remote_measure_command(
            seed_cell, args.storage, database_ip, postgres_password, tools_image
        )
        result = gcloud_ssh(loadgen_instance, args.zone, args.project_id, measure_cmd)
        payload = _parse_storage_result(result.stdout, result.stderr)

        STORAGE_OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = STORAGE_OUT_DIR / f"{args.storage}.json"
        out_path.write_text(json.dumps(payload, indent=2))
        print(f"\nArmazenamento real medido e gravado em {out_path}: {json.dumps(payload)}")

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
                f"terraform destroy do root de seed (storage={args.storage}) — é isso que PARA "
                "a cobrança."
            )
            os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = fetch_terraform_access_token(args.project_id)
            terraform(
                [
                    "destroy",
                    "-auto-approve",
                    f"-var=project_id={args.project_id}",
                    f"-var=region={args.region}",
                    f"-var=zone={args.zone}",
                    f"-var=storage={args.storage}",
                    f"-var=tools_image={tools_image}",
                    f"-var=dataset_bucket={args.dataset_bucket}",
                    f"-var=results_bucket={args.results_bucket}",
                    f"-var=data_disk_snapshot={data_disk_snapshot}",
                ],
                tf_dir=TF_DIR,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
