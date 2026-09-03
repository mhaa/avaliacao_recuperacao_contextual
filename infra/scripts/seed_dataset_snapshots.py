#!/usr/bin/env python3
"""Semeia o dataset completo uma vez por tecnologia de banco e tira um
snapshot de disco fixo, reusado automaticamente por
infra/scripts/run_measurement_battery.py (snapshot_exists()) — evita
recarregar postgres/scylla/opensearch do zero a cada execução de
triagem/confirmação (README.md, "Fase 5"). Valkey fica de fora: roda 100%
em memória, sem disco persistente (--save "" --appendonly no, ver
docker-compose.yml e CLAUDE.md) — não há nada pra tirar snapshot.

A carga completa é idêntica entre todas as células de uma mesma tecnologia
de banco (load_full_dataset.py não recebe estratégia nenhuma) — então um
snapshot por storage cobre as 3 ou 4 células que compartilham esse banco.

Roda no HOST, mesmo motivo de cloud_smoke_test.py/run_measurement_battery.py:
usa infra/envs/seed/ — réplica enxuta de infra/envs/experiment (só
network + database + loadgen, sem service, que não é necessário pra
carregar dado) — e SSH via IAP na VM de loadgen pra rodar o mesmo setup
remoto que a bateria de medição usa (build_remote_setup_command, sempre
com skip_dataset_load=False: aqui é exatamente onde a carga completa
precisa acontecer).

O nome do snapshot é fixo por storage ("tcc-dataset-seed-<storage>"), não
timestampado: é sempre "a semente atual" dessa tecnologia. Rodar de novo
substitui o snapshot anterior (apaga o antigo, com confirmação, antes de
criar o novo).

Uso (via -m, mesmo motivo de run_measurement_battery.py — este módulo
importa infra.scripts.* por nome absoluto):
    python -m infra.scripts.seed_dataset_snapshots <storage> <project-id> <region> <zone> \\
        <terraform-state-bucket> <dataset-bucket> [--keep-infra]

    # storage: postgres, scylla ou opensearch (nunca valkey).

Cada comando faturável (terraform apply/destroy) e a exclusão de um
snapshot antigo são anunciados explicitamente antes de rodar e pedem
confirmação — mesma regra permanente de cloud_smoke_test.py, pedida pelo
usuário.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from infra.scripts.cloud_smoke_test import (
    _confirm_billable,
    _resolve_cmd,
    _run,
    fetch_terraform_access_token,
    gcloud_ssh,
    terraform,
    terraform_output_json,
    wait_for_container,
)
from infra.scripts.run_measurement_battery import build_remote_setup_command, snapshot_exists

# gcloud espera (poll) a operação de snapshot terminar antes de devolver —
# mas o timeout de LEITURA do cliente (confirmado ao vivo: 300s,
# HTTPSConnectionPool ReadTimeout) é mais curto que o tempo real de um
# snapshot de 200 GB. Isso já causou o bug real que esta constante e
# wait_for_snapshot_ready existem para corrigir: o cliente desistia e saía
# com erro enquanto o SERVIDOR continuava criando o snapshot com sucesso —
# e o script, confiando só no código de saída do gcloud, seguia direto para
# o destroy do disco de origem achando que tinha falhado. Nunca confiar no
# exit code do cliente para essa operação — só no `status` real, via poll.
_SNAPSHOT_POLL_INTERVAL_S = 15
_SNAPSHOT_POLL_TIMEOUT_S = 1800  # 30 min — folgado sobre os ~300s já vistos falhar.

TF_DIR = "infra/envs/seed"
VALID_STORAGES = ("postgres", "scylla", "opensearch")
# Mesmo diretório de bind mount usado por run_measurement_battery.py —
# só precisa sobreviver ao `docker run --rm` do setup remoto, nunca é lido
# depois (a bateria de medição de verdade roda numa célula separada).
FIXTURES_MOUNT = "/home/tcc/load-fixtures"


def snapshot_name_for(storage: str) -> str:
    return f"tcc-dataset-seed-{storage}"


def delete_existing_snapshot(project_id: str, snapshot_name: str) -> None:
    """Apaga um snapshot antigo do mesmo nome, se existir — precisa vir
    antes de criar o novo (gcloud recusa reusar o nome de um snapshot
    vivo). Pede confirmação: mesmo sendo dado regenerável (é só a carga
    completa de novo), apagar é uma ação destrutiva de verdade."""
    if not snapshot_exists(project_id, snapshot_name):
        return
    _confirm_billable(
        f"o snapshot '{snapshot_name}' já existe e será APAGADO antes de criar a versão nova "
        "— isso não é reversível."
    )
    _run(
        [
            "gcloud",
            "compute",
            "snapshots",
            "delete",
            snapshot_name,
            f"--project={project_id}",
            "--quiet",
        ]
    )


def _snapshot_status(project_id: str, snapshot_name: str) -> str:
    """Status real do snapshot no SERVIDOR — "" se ainda não existe (o
    objeto pode levar um instante para aparecer depois do comando de
    criação retornar/falhar do lado do cliente). Nunca levanta: consulta
    de leitura usada em loop de polling, uma falha transitória de rede
    aqui não deve derrubar o polling inteiro."""
    cmd = [
        "gcloud",
        "compute",
        "snapshots",
        "describe",
        snapshot_name,
        f"--project={project_id}",
        "--format=value(status)",
    ]
    print(f"+ {' '.join(cmd)}")
    result = subprocess.run(
        _resolve_cmd(cmd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.stdout.strip()


def wait_for_snapshot_ready(project_id: str, snapshot_name: str) -> None:
    """Espera o snapshot chegar a READY consultando o SERVIDOR — nunca
    confia no código de saída do `gcloud compute disks snapshot` que o
    disparou, porque esse comando pode dar timeout de leitura no cliente
    (confirmado ao vivo, ~300s, disco de 200 GB) enquanto a operação segue
    e termina com sucesso no servidor. É essa distinção — cliente que
    desistiu de esperar vs. servidor que realmente falhou — que decide se é
    seguro destruir o disco de origem em seguida.

    Levanta RuntimeError com uma mensagem acionável se o snapshot terminar
    em estado de erro ou não chegar a READY dentro do timeout. Quem chama
    ainda vai rodar o destroy da VM no `finally` (parar de cobrar é
    prioridade mesmo com o snapshot em dúvida) — mas precisa saber, sem
    ambiguidade, que os dados carregados NÃO estão preservados e a carga
    completa precisa ser refeita.
    """
    deadline = time.monotonic() + _SNAPSHOT_POLL_TIMEOUT_S
    last_status = ""
    while time.monotonic() < deadline:
        last_status = _snapshot_status(project_id, snapshot_name)
        if last_status == "READY":
            return
        if last_status in ("FAILED", "DELETING"):
            raise RuntimeError(
                f"snapshot '{snapshot_name}' terminou em estado '{last_status}' no servidor "
                "— não é um timeout de cliente, é falha real. Os dados carregados NÃO estão "
                "preservados; a carga completa (schema + load_full_dataset.py) precisa ser "
                "refeita antes de tentar o snapshot de novo."
            )
        time.sleep(_SNAPSHOT_POLL_INTERVAL_S)
    status_desc = last_status if last_status else "inexistente"
    raise RuntimeError(
        f"snapshot '{snapshot_name}' não chegou a READY em {_SNAPSHOT_POLL_TIMEOUT_S}s de "
        f"polling (último status visto: '{status_desc}'). Verifique manualmente com "
        f"'gcloud compute snapshots describe {snapshot_name} --project={project_id}' antes "
        "de assumir qualquer coisa sobre os dados."
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
        "--keep-infra",
        action="store_true",
        help="não roda terraform destroy no final (para investigar uma falha na carga)",
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

    snapshot_name = snapshot_name_for(args.storage)
    seed_cell = f"seed-{args.storage}"

    print("Mintando token de acesso via impersonação da SA do Terraform (nunca fica em disco)...")
    os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = fetch_terraform_access_token(args.project_id)

    applied = False
    try:
        _confirm_billable(
            f"terraform apply do root de seed (storage={args.storage}) em "
            f"{args.project_id}/{args.region} vai criar VMs reais (banco + loadgen) e carregar "
            "o dataset completo — pode levar bastante tempo e cobra o tempo todo."
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
            ],
            tf_dir=TF_DIR,
        )

        outputs = terraform_output_json(tf_dir=TF_DIR)
        database_ip = outputs["database_internal_ip"]
        data_disk_name = outputs["data_disk_name"]
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

        setup_cmd = build_remote_setup_command(
            seed_cell,
            args.storage,
            database_ip,
            # service_ip: root de seed não tem módulo `service` (schema+
            # carga só fala com o banco) — build_storage_env_flags usa
            # isso só pra montar TEST_SERVICE_URL, uma env var que o
            # setup remoto nunca lê. database_ip aqui é só um valor
            # inofensivo, não um IP de serviço de verdade.
            database_ip,
            tools_image,
            postgres_password,
            FIXTURES_MOUNT,
            args.dataset_bucket,
        )
        gcloud_ssh(loadgen_instance, args.zone, args.project_id, setup_cmd)

        print(f"\nCarga completa concluída em {database_instance}. Tirando snapshot do disco...")
        delete_existing_snapshot(args.project_id, snapshot_name)
        try:
            _run(
                [
                    "gcloud",
                    "compute",
                    "disks",
                    "snapshot",
                    data_disk_name,
                    f"--zone={args.zone}",
                    f"--project={args.project_id}",
                    f"--snapshot-names={snapshot_name}",
                ]
            )
        except subprocess.CalledProcessError as exc:
            # Não relança ainda: o comando acima FAZ POLL da operação até
            # terminar, então um erro aqui pode ser só o timeout de leitura
            # do CLIENTE (visto ao vivo: ReadTimeout em ~300s contra um
            # disco de 200 GB) com o SERVIDOR seguindo e terminando com
            # sucesso. wait_for_snapshot_ready abaixo consulta o status real
            # e decide — nunca o exit code sozinho.
            print(
                f"\nAVISO: 'gcloud compute disks snapshot' retornou erro no cliente "
                f"({exc}) — isso pode ser só o cliente desistindo de esperar, não o "
                "servidor falhando. Consultando o status real do snapshot..."
            )
        wait_for_snapshot_ready(args.project_id, snapshot_name)
        print(
            f"\nSnapshot '{snapshot_name}' confirmado READY no servidor — "
            f"run_measurement_battery.py vai usá-lo automaticamente em qualquer célula com "
            f"storage={args.storage}."
        )

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
                "a cobrança. O snapshot já criado sobrevive independente disso."
            )
            # Re-minta o token (~1h de validade) antes do destroy final: a
            # carga completa do dataset (setup_cmd acima) pode facilmente
            # levar mais que isso — confirmado ao vivo, o destroy falhou com
            # HTTP 401 ("Authentication required") no backend GCS do
            # Terraform depois de ~3h30 de carga, deixando as VMs de
            # seed-scylla no ar e cobrando até serem destruídas manualmente.
            # Mesmo fix já aplicado em run_measurement_battery.py.
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
                ],
                tf_dir=TF_DIR,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
