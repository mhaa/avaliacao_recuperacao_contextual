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

Credencial do Terraform: um token de acesso de curta duração (~1h) mintado
por impersonação da SA `terraform-tcc` (roles/iam.serviceAccountTokenCreator,
concedido por infra/scripts/create_terraform_service_account.sh) no início
da execução — nunca um arquivo de chave, nem no Secret Manager, nem em
disco. Usa a sessão pessoal do operador (`gcloud auth login`) para mintar
o token; a própria service account do Terraform não pode impersonar a si
mesma (seria circular).

Uso:
    python infra/scripts/cloud_smoke_test.py <cell> <project-id> <region> <zone> \\
        <terraform-state-bucket> <dataset-bucket> [--keep-infra]

Cada comando faturável (terraform apply/destroy) é anunciado explicitamente
antes de rodar e pede confirmação — mesmo com o plano já aprovado (regra
permanente pedida pelo usuário).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

REPO_ROOT_RELATIVE_TF_DIR = "infra/envs/experiment"
TERRAFORM_SA_TEMPLATE = "terraform-tcc@{project_id}.iam.gserviceaccount.com"
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


def _print_safe(text: str) -> None:
    """`print()` puro quebra com `UnicodeEncodeError` no console do Windows
    (cp1252) quando o texto (stdout/stderr capturado de um comando remoto
    Linux) tem caracteres fora dessa codificação — confirmado ao vivo: o
    próprio `print` de diagnóstico de erro de `_run` (abaixo) mascarava a
    causa raiz real ao travar tentando exibi-la. Reencodar com
    errors="replace" contra a codificação real do stdout evita isso sem
    perder a legibilidade do texto original."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding))


def _resolve_cmd(cmd: list[str]) -> list[str]:
    """No Windows, `gcloud`/`docker` são wrappers `.cmd`/`.exe` — CreateProcess
    (usado por subprocess.run com shell=False) não os acha pelo nome puro,
    só cmd.exe faz essa busca por PATHEXT. shutil.which resolve isso de
    forma portátil (funciona igual em Linux/Mac, onde já resolvia por PATH
    de qualquer forma), sem precisar de shell=True — que exigiria escapar
    cada argumento manualmente."""
    resolved = shutil.which(cmd[0])
    return [resolved, *cmd[1:]] if resolved else cmd


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"+ {' '.join(cmd)}")
    if kwargs.get("text"):
        # Sem isso, subprocess decodifica com locale.getpreferredencoding()
        # — no Windows isso é cp1252, e a saída remota (pytest/k6 rodando
        # em Linux) pode conter bytes UTF-8 que não são cp1252 válido,
        # derrubando a thread leitora com UnicodeDecodeError e perdendo o
        # resultado inteiro dos gates (confirmado rodando de verdade
        # contra e1-opensearch).
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    try:
        return subprocess.run(_resolve_cmd(cmd), check=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        # Sem isso, um `gcloud compute ssh --command=...` que falha do lado
        # remoto (ex.: load/run_battery.py quebrando dentro do container)
        # nunca mostra POR QUE — capture_output=True guarda stdout/stderr no
        # CompletedProcess, mas o traceback padrão de CalledProcessError só
        # imprime "returned non-zero exit status", nunca o conteúdo real.
        # Confirmado ao vivo: uma falha remota real ficou completamente sem
        # pista nenhuma até este fix.
        if exc.stdout:
            _print_safe(f"--- stdout do comando que falhou ---\n{exc.stdout}")
        if exc.stderr:
            _print_safe(f"--- stderr do comando que falhou ---\n{exc.stderr}")
        raise


def _confirm_billable(message: str) -> None:
    print()
    print("=" * 70)
    print(f"FATURÁVEL: {message}")
    print("=" * 70)
    answer = input("Digite 'sim' para continuar, qualquer outra coisa para abortar: ")
    if answer.strip().lower() != "sim":
        print("Abortado pelo usuário.")
        sys.exit(1)


def fetch_terraform_access_token(project_id: str) -> str:
    """Minta um token de acesso de curta duração (~1h) impersonando a SA do
    Terraform — nunca um arquivo de chave. Exige que a sessão pessoal do
    operador já tenha roles/iam.serviceAccountTokenCreator sobre essa SA
    (create_terraform_service_account.sh concede isso)."""
    sa_email = TERRAFORM_SA_TEMPLATE.format(project_id=project_id)
    result = _run(
        ["gcloud", "auth", "print-access-token", f"--impersonate-service-account={sa_email}"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def terraform(args: list[str], tf_dir: str = REPO_ROOT_RELATIVE_TF_DIR) -> None:
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
        f"-chdir={tf_dir}",
        *args,
    ]
    _run(cmd)


def terraform_output_json(tf_dir: str = REPO_ROOT_RELATIVE_TF_DIR) -> dict:
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
        f"-chdir={tf_dir}",
        "output",
        "-json",
    ]
    print(f"+ {' '.join(cmd)}")
    result = subprocess.run(
        _resolve_cmd(cmd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    parsed = json.loads(result.stdout)
    return {k: v["value"] for k, v in parsed.items()}


def gcloud_ssh(
    instance: str, zone: str, project_id: str, remote_command: str
) -> subprocess.CompletedProcess:
    # base64 + "bash -c 'echo ... | base64 -d | bash'": blinda o valor de
    # --command= contra o cmd.exe do Windows, que a invocação de
    # gcloud.CMD (um wrapper .bat) força no meio do caminho (Python ->
    # cmd.exe -> plink.exe -> sshd remoto). cmd.exe trata &, &&, ;, {, }
    # como separadores de comando mesmo dentro do que o Python considera
    # "um único argumento" — aspas simples do bash não protegem nada
    # disso, só aspas duplas (e só parcialmente). Confirmado ao vivo: um
    # remote_command com esses caracteres soltos fora de um único bloco
    # `-c '...'` chegava fragmentado — parte interpretada localmente pelo
    # cmd.exe ("'{' não é reconhecido..."), parte corrompida no bash
    # remoto ("sleep: invalid time interval"). base64 não tem nenhum
    # caractere especial de shell, então nada sobra pro cmd.exe mangle
    # antes de chegar no bash remoto, que decodifica e executa de volta.
    encoded = base64.b64encode(remote_command.encode()).decode()
    wrapped_command = shlex.join(["bash", "-c", f"echo {encoded} | base64 -d | bash"])
    cmd = [
        "gcloud",
        "compute",
        "ssh",
        instance,
        f"--zone={zone}",
        f"--project={project_id}",
        "--tunnel-through-iap",
        f"--command={wrapped_command}",
    ]
    # input="y\n": no Windows, `gcloud compute ssh` usa plink.exe, que pede
    # confirmação interativa ("Store key in cache?") na PRIMEIRA conexão a
    # cada VM nova (cada célula/seed cria VMs com nome novo, então isso
    # acontece toda vez). Sem stdin real anexado (rodando via subprocess a
    # partir de um script Python em background), esse prompt nunca é
    # respondido e o plink derruba a conexão sozinho após ficar pendurado —
    # confirmado ao vivo: a sessão ficou ~3h30 "rodando" em silêncio antes
    # de morrer com "Remote side unexpectedly closed network connection",
    # bem no meio da carga completa do dataset no Scylla em us-east4.
    # "y\n" aceita a host key automaticamente (equivalente a
    # StrictHostKeyChecking=accept-new do OpenSSH); é inofensivo em
    # conexões subsequentes, onde a chave já está em cache e o prompt não
    # aparece — a entrada extra não é lida por ninguém.
    return _run(cmd, capture_output=True, text=True, input="y\n")


def wait_for_service_ready(
    instance: str, zone: str, project_id: str, service_ip: str, timeout_s: int = 600
) -> None:
    """Espera o serviço RESPONDER, não só o container existir.

    `wait_for_container` confirma que o processo subiu; isso não é a mesma
    coisa que o banco por trás dele já servir consultas. Confirmado ao vivo
    no OpenSearch: nos primeiros ~70-90s de cada deploy novo, 100% das
    requisições voltavam HTTP 500 (cluster ainda alocando shards) — em
    e1-opensearch as falhas iam de 21:09:17 até 21:10:31, quando veio o
    primeiro 200. Ficava tudo dentro do cenário `warmup`, que
    analysis/collect.py descarta, então os percentis não eram corrompidos;
    mas os 2 min de aquecimento viravam ~40s de aquecimento real, e um
    startup um pouco mais lento faria o erro vazar para o cenário
    `measurement`.

    Genérico de propósito (não um remendo só do OpenSearch): exercita
    exatamente o caminho que o gerador vai usar — POST no endpoint real,
    através do serviço, até o banco. Um usuário sem candidatos devolve 200
    com lista vazia, então serve como sonda de prontidão para as quatro
    tecnologias sem depender de qual dado foi carregado.
    """
    print(f"Aguardando o serviço em {service_ip}:8000 responder a uma requisição real...")
    deadline = time.monotonic() + timeout_s
    payload = '{"user_id":1,"context":[],"exclude":[],"k":20}'
    # `|| echo 000`: sem isso, curl sai != 0 enquanto o serviço ainda não
    # aceita conexão e o _run(check=True) de gcloud_ssh derrubaria a
    # execução inteira no primeiro poll — o normal no começo do boot.
    check_cmd = (
        f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 10 "
        f"-X POST http://{service_ip}:8000/v1/recommendations "
        f"-H 'Content-Type: application/json' -d '{payload}' || echo 000"
    )
    last_status = "(nenhuma resposta ainda)"
    while time.monotonic() < deadline:
        try:
            result = gcloud_ssh(instance, zone, project_id, check_cmd)
            out = result.stdout.strip()
            last_status = out.splitlines()[-1] if out else "(vazio)"
            if last_status.endswith("200"):
                print("  serviço respondendo 200 a uma requisição real.")
                return
        except Exception as exc:  # noqa: BLE001 — SSH falha transitoriamente no boot
            last_status = f"(SSH falhou: {exc})"
        time.sleep(10)
    raise TimeoutError(
        f"serviço em {service_ip}:8000 não respondeu 200 em {timeout_s}s "
        f"(último status: {last_status}). Verifique `docker logs tcc-service` na VM de serviço "
        "e `docker logs tcc-database` na VM de banco."
    )


def tail_remote_file(
    instance: str, zone: str, project_id: str, path: str, lines: int = 40
) -> str:
    """Poll leve e não-bloqueante do progresso de um comando remoto ainda
    em andamento (ex.: build_remote_setup_command com carga completa, que
    grava em `path` via `tee` — ver run_measurement_battery.py). Abre uma
    sessão SSH SEPARADA da principal — múltiplas sessões concorrentes pela
    mesma VM via IAP funcionam normalmente — só para ler as últimas linhas
    do log, sem esperar o comando principal (que pode levar horas)
    terminar. Existe porque gcloud_ssh()/capture_output=True só devolve
    stdout quando o processo SSH inteiro sai, então sem isso não há como
    diferenciar "ainda trabalhando" de "travado" enquanto o comando
    principal está bloqueado — exatamente o problema que derrubou ~3h30 de
    carga do e1-scylla em us-east4 sem nenhum sinal de vida."""
    result = gcloud_ssh(
        instance,
        zone,
        project_id,
        f"tail -n {lines} {path} 2>/dev/null || echo '(log ainda não existe)'",
    )
    return result.stdout


def wait_for_container(
    instance: str, zone: str, project_id: str, container_name: str, timeout_s: int = 600
) -> None:
    # 600s, não 300s: confirmado ao vivo com e1-scylla — este check só
    # confirma que o CONTAINER está "running", não que o banco por trás dele
    # já aceita conexões de verdade. Scylla (motor JVM/nativo) demora mais
    # que Postgres/Valkey pra terminar seu próprio bootstrap interno; nesse
    # meio tempo o container `tcc-service` (que conecta assim que sobe, sem
    # esperar o banco) cai com `NoHostAvailable` e reinicia via `--restart
    # unless-stopped` até o Scylla ficar pronto — o serviço realmente ficou
    # saudável, só que depois da janela de 300s ter esgotado por azar de
    # timing entre o ciclo de crash-reinício e o polling deste check.
    print(f"Aguardando container '{container_name}' em {instance}...")
    deadline = time.monotonic() + timeout_s
    check_cmd = f"docker ps --filter name={container_name} --filter status=running -q"
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                _resolve_cmd(
                    [
                        "gcloud",
                        "compute",
                        "ssh",
                        instance,
                        f"--zone={zone}",
                        f"--project={project_id}",
                        "--tunnel-through-iap",
                        f"--command={check_cmd}",
                    ]
                ),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
            if result.stdout.strip():
                print(f"  {container_name} no ar.")
                return
        except subprocess.TimeoutExpired:
            pass
        time.sleep(10)
    raise TimeoutError(f"container '{container_name}' não subiu em {timeout_s}s")


def build_storage_env_flags(
    cell_id: str, storage: str, database_ip: str, service_ip: str, postgres_password: str | None
) -> list[str]:
    """Variáveis de ambiente para o container remoto falar com o banco/serviço
    de uma célula — mesma convenção de nomes já usada em docker-compose.yml,
    sem inventar uma segunda fonte. Compartilhado entre o smoke test (aqui) e
    a bateria de medição real (infra/scripts/run_measurement_battery.py,
    Fase 5) — único lugar que sabe montar essas variáveis por storage."""
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
    env_flags += [f"TEST_SERVICE_URL=http://{service_ip}:8000"]
    return env_flags


def build_schema_and_fixture_steps(storage: str, mode: str = "oracle") -> list[str]:
    """Passos para aplicar o schema (quando existir) e carregar o dado —
    mesma ordem usada pelo smoke test (mode="oracle", default: só o
    subconjunto de ~904 usuários referenciados pelo oráculo) e pela bateria
    real de Fase 5 (mode="full", infra/scripts/run_measurement_battery.py:
    a base inteira — load/zipf.js amostra de toda a população, o
    subconjunto do oráculo corromperia silenciosamente cada latência
    medida)."""
    schema_step = STORAGES_WITH_SCHEMA_APPLY.get(storage)
    steps = []
    if schema_step:
        steps.append(f"python {schema_step}")
    loader = "load_oracle_fixture.py" if mode == "oracle" else "load_full_dataset.py"
    steps.append(f"python schemas/{storage}/{loader}")
    return steps


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
    env_flags = build_storage_env_flags(cell_id, storage, database_ip, service_ip, postgres_password)
    steps = build_schema_and_fixture_steps(storage)
    # Gate 1: strategy + storage adapter em processo, sem rede — prova a
    # lógica de recuperação, mas nunca passa pelo transporte HTTP.
    steps.append(f"python -m harness.verify_cli --cell {cell_id}")
    # Gate 2: mesmos casos do oráculo (uma amostra — ver
    # tests/acceptance/test_service_smoke.py), agora via POST HTTP de
    # verdade contra o serviço da célula. Sem isso, um bug só no parsing
    # de Request/Response ou na serialização do service/http_app.py passa
    # batido: o Gate 1 não o alcança, e o smoke k6 (abaixo) só confere
    # status HTTP 200, nunca o conteúdo da resposta.
    steps.append("python -m pytest -m integration tests/acceptance/test_service_smoke.py -v")
    steps.append("python load/export_contexts_by_tier.py")
    steps.append(
        f"SMOKE_MODE=true TARGET_URL=http://{service_ip}:8000/v1/recommendations "
        f"CELL={cell_id} k6 run load/scenarios.js --out json=/tmp/smoke-raw.json "
        "--console-output=/tmp/smoke-requests.ndjson --log-format=raw"
    )
    steps.append("python analysis/smoke_report.py /tmp/smoke-requests.ndjson")
    inner = " && ".join(steps)

    docker_argv = ["docker", "run", "--rm", "--network", "host", "--entrypoint", "bash"]
    for flag in env_flags:
        docker_argv += ["-e", flag]
    docker_argv += [tools_image, "-c", inner]
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
    # A codepage padrão do console do Windows (cp1252) não representa vários
    # caracteres que aparecem na saída de k6/docker/pytest vindos da VM Linux
    # remota via SSH — confirmado ao vivo: print(result.stdout) do smoke test
    # derrubou o script com UnicodeEncodeError DEPOIS do teste remoto já ter
    # passado e da infraestrutura já ter sido destruída no finally, mascarando
    # um resultado bom como falha. errors="replace" garante que nenhum print
    # deste script derruba a execução por causa de um caractere isolado.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cell")
    parser.add_argument("project_id")
    parser.add_argument("region")
    parser.add_argument("zone")
    parser.add_argument("terraform_state_bucket")
    parser.add_argument(
        "dataset_bucket",
        help="output do bootstrap (dataset_bucket) — obrigatório em infra/envs/experiment "
        "mesmo aqui: o smoke test usa mode=\"oracle\" (nunca baixa nada do bucket), mas "
        "module.loadgen precisa do valor pra criar a IAM binding.",
    )
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

    print("Mintando token de acesso via impersonação da SA do Terraform (nunca fica em disco)...")
    os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = fetch_terraform_access_token(args.project_id)

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
                # -reconfigure: infra/ é bind mount (README.md, Etapa 9) —
                # o .terraform/ local ainda lembra o backend da célula
                # anterior. Cada prefixo por célula é deliberadamente uma
                # localização isolada nova, nunca uma migração de estado
                # — confirmado rodando smoke test em duas células
                # seguidas, sem isso o init recusa trocar de prefixo.
                "-reconfigure",
                f"-backend-config=bucket={args.terraform_state_bucket}",
                f"-backend-config=prefix=cells/{args.cell}",
            ]
        )
        _phase("terraform apply")
        terraform(
            [
                "apply",
                "-auto-approve",
                f"-var=project_id={args.project_id}",
                f"-var=region={args.region}",
                f"-var=zone={args.zone}",
                f"-var=cell={args.cell}",
                f"-var=storage={storage}",
                f"-var=dataset_bucket={args.dataset_bucket}",
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
                    "-auto-approve",
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
