#!/usr/bin/env python3
"""Bateria de medição real em nuvem — README.md, "Fase 5". Contraparte de
infra/scripts/cloud_smoke_test.py (Fase 4): em vez de um smoke curto, roda
load/run_battery.py de verdade contra a célula, varrendo carga ×
seletividade conforme docs/DESIGN.md ("Protocolo de medição" /
"Delineamento em duas etapas"), e sobe `results/` direto da VM loadgen para
o bucket de resultados (load/upload_results.py, via ADC/metadata server —
não passa mais pelo host do operador). Também roda
a busca de vazão de saturação (load/saturation.py), cujo `S` é internalizado
no custo da fronteira de Pareto 2D via `n(D) = ⌈D/S⌉` (analysis/pareto.py):
rampa curta na triagem (passos de 25%, 2 min por sondagem, busca binária ao
violar o SLO, teto de 50.000 req/s e 5 repetições do patamar aprovado), rampa
fina de confirmação (5 repetições por patamar, incrementos de 10%) só nas
células não dominadas.

Roda no HOST (não dentro do container `tools`), pelo mesmo motivo de
cloud_smoke_test.py: a imagem `tools` não tem o `gcloud` CLI, só
terraform+k6+Python — e agora também precisa de `google-cloud-monitoring`
no host, para `analysis.resources.GCPMonitoringCollector` amostrar
CPU/memória/rede das 3 VMs a cada 5s durante o sweep de confirmação
(`resources.csv`, README.md, "Instrumentação de gargalo"). A CPU do gerador
a cada patamar da busca de saturação NÃO usa esse caminho — vem de
`/proc/stat` lido na própria VM loadgen, dentro do mesmo processo remoto que
já decide o veredito da sondagem (analysis/probe_report.py), sem depender do
Cloud Monitoring. Terraform roda via docker-compose.gcp.yml; a bateria de
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
    # rampa fina, docs/DESIGN.md), nunca na triagem.
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
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from infra.scripts.cloud_smoke_test import (
    _confirm_billable,
    _resolve_cmd,
    _run,
    build_schema_and_fixture_steps,
    build_storage_env_flags,
    fetch_terraform_access_token,
    gcloud_ssh,
    gcloud_ssh_with_retry,
    resource_snapshot,
    restart_container,
    storage_for_cell,
    terraform,
    terraform_output_json,
    wait_for_container,
    wait_for_service_ready,
)
from load.run_battery import build_probe_k6_cmd
from load.saturation import (
    BINARY_SEARCH_ITERATIONS,
    GENERATOR_CPU_THRESHOLD,
    ProbeResult,
    run_saturation_search,
)

# docs/DESIGN.md, "Protocolo de medição". Só os dois primeiros níveis são
# fixos — o 3º nível ("alto") da confirmação não é mais um valor fixo (era
# 10_000): passa a ser a vazão de saturação medida pela própria rampa de
# confirmação de cada seletividade (main(), high_rate_by_tier), porque
# 10.000 req/s já testava direto a região de falha profunda para as células
# medidas, sem discriminar nada. build_sweep() monta o 3º nível a partir daí.
FIXED_LOAD_LEVELS = [100, 1_000]
# Fallback só para o caso raro de uma rampa de confirmação terminar em
# gargalo do gerador (loadgen_bottleneck=True) sem nenhum S nem lower_bound
# utilizável — sem isso o nível "alto" dessa seletividade ficaria sem valor.
# Ver _high_rate_from_saturation.
LEGACY_HIGH_RATE_FALLBACK = 10_000
SELECTIVITY_TIERS = ["high", "medium", "low"]
# docs/DESIGN.md, "Parâmetros fixos": U da medição principal — a base real
# completa do MovieLens 32M, que é o que build_remote_setup_command carrega
# (mode="full"). Injetado como USER_COUNT no k6 (load/zipf.js) em TODA
# combinação e sondagem: sem isso o Zipf usa o default dev-scale (10.000) e
# amostra ~5% da base carregada — foi o que invalidou a primeira triagem.
# Sobrescreva com --user-count apenas na varredura de escalabilidade
# (U sintético de 1-3 milhões).
MAIN_MEASUREMENT_USER_COUNT = 200_948
# docs/DESIGN.md, "Delineamento em duas etapas": a triagem roda com seletividade
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

# Repetições por patamar na rampa de confirmação (docs/DESIGN.md, item pedido
# pelo usuário) — a rampa curta usa 1 (ensaio único, por isso a tolerância
# de 20% em analysis/pareto.py).
CONFIRMATION_REPETITIONS = 5
CONFIRMATION_WARMUP = "2m"
CONFIRMATION_MEASURE = "3m"
SHORT_RAMP_WARMUP = "0s"
# 2m, não 1m: S entra direto em n(D) = ⌈D/S⌉ (analysis/pareto.py), ou seja, no
# custo — uma janela curta demais deixa ruído de medição virar erro de custo.
SHORT_RAMP_MEASURE = "2m"
# 25% em vez de dobras: com dobras, o patamar de violação podia cair ao dobro
# do valor real, e a busca binária tinha de recuperar toda essa faixa.
SHORT_RAMP_STEP = 0.25
# Repetições do patamar aprovado ao fim da rampa curta — é o que tira o S de
# "ensaio único" (docs/DESIGN.md) sem multiplicar a busca inteira. 5 para
# bater com o resto do protocolo (carga fixa e rampa de confirmação também
# usam 5); com 3, um IC por bootstrap não se sustentaria.
SHORT_RAMP_CONFIRM_REPETITIONS = 5

# Teto de segurança de iterações da busca binária quando --saturation-min-step
# está ativo (load/saturation.py:_binary_search) — nesse modo a busca para
# por LARGURA de intervalo, não por contagem fixa, então o teto de
# iterações vira só uma rede de segurança contra um veredito
# instável/oscilante que nunca convirja. Generoso o bastante para os
# intervalos típicos da rampa fina (passo de 10% a partir de ~1.000-5.000
# req/s): log2(largura_inicial / min_step) raramente passa de ~10-12 para
# esses valores.
SATURATION_MIN_STEP_ITERATION_CEILING = 20

# docs/ARCHITECTURE.md, "Topologia": memória nominal dos tipos de máquina
# padrão — só para converter a fração que o coletor OpenTelemetry reporta
# em MB (analysis/resources.py:GCPMonitoringCollector), sem uma chamada
# extra à API do Compute para descobrir o tipo de máquina em runtime. Se
# `machine_type` for sobrescrito em terraform.tfvars, ajustar aqui também.
DEFAULT_MEMORY_MB_BY_COMPONENT = {
    "database": 32768.0,  # n2-standard-8
    "service": 32768.0,  # n2-standard-8
    "loadgen": 32768.0,  # n2-standard-8
}

# Amostragem periódica de recursos durante a rampa de confirmação
# (docs/DESIGN.md, "Atribuição de gargalo"). Bug real, achado ao vivo
# 2026-09-16: as 5 execuções da confirmação rodaram inteiras sem escrever
# `resources.csv` porque RESOURCE_SAMPLE_INTERVAL_SECONDS também definia a
# LARGURA da janela consultada (5s) — o Cloud Monitoring recusa mais de 1
# ponto por minuto para métricas customizadas (`workload.googleapis.com/*`,
# ver infra/modules/database/main.tf), então uma janela de 5s nunca continha
# os 2 pontos necessários para calcular uma taxa de CPU
# (analysis/resources.py:_cpu_utilization_from_deltas). Tick e janela agora
# são desacoplados: RESOURCE_SAMPLE_INTERVAL_SECONDS só controla o intervalo
# de verificação (não precisa ser fino — o dado em si só muda a cada ~60s);
# RESOURCE_QUERY_WINDOW_SECONDS é a largura real pedida ao Cloud Monitoring
# (2,5x o piso de 60s, folga para 2+ pontos mesmo com jitter de ingestão);
# RESOURCE_INGESTION_DELAY_SECONDS afasta o fim da janela de "agora" (o
# ponto mais recente de uma métrica customizada normalmente ainda não foi
# ingerido no instante exato em que é gerado).
RESOURCE_SAMPLE_INTERVAL_SECONDS = 30
RESOURCE_QUERY_WINDOW_SECONDS = 150
RESOURCE_INGESTION_DELAY_SECONDS = 30


def build_sweep(
    phase: str,
    high_rate_by_tier: dict[str, int] | None = None,
    tiers: list[str] | None = None,
) -> list[tuple[int, str]]:
    """`tiers`: escopo de seletividades da bateria de carga fixa (default
    None = SELECTIVITY_TIERS, as 3) — usado por --tier para restringir uma
    re-medição a uma seletividade só (docs/DESIGN.md, re-medição de
    seletividade média). Chamadas existentes sem `tiers` continuam com a
    cross-product completa de sempre."""
    tiers = tiers if tiers is not None else SELECTIVITY_TIERS
    if phase == "triagem":
        return [(TRIAGEM_RATE, TRIAGEM_TIER)]
    if phase == "confirmacao":
        if high_rate_by_tier is None:
            raise ValueError(
                "build_sweep('confirmacao') exige high_rate_by_tier — o nível de carga "
                "'alto' vem da rampa de confirmação de cada seletividade, rodada antes."
            )
        return [(rate, tier) for rate in FIXED_LOAD_LEVELS for tier in tiers] + [
            (high_rate_by_tier[tier], tier) for tier in tiers
        ]
    raise ValueError(f"fase desconhecida: {phase!r}")


def _high_rate_from_saturation(saturation: SaturationSearchResult, tier: str) -> int:
    """Deriva o nível de carga 'alto' da confirmação a partir da rampa de
    saturação daquela seletividade (docs/DESIGN.md, "Protocolo de medição").
    Caso normal: o S aproximado. Célula censurada nessa seletividade: usa o
    lower_bound — por definição a rampa não violou o SLO até esse teto, ainda
    não é região de falha. Gargalo do gerador (nem approx nem lower_bound
    utilizável): não há nenhum S confiável dessa rampa — cai no fallback fixo
    antigo, com aviso explícito (caso raro numa célula que já passou pela
    triagem, mas sem isso o nível 'alto' ficaria sem valor)."""
    if saturation.approx_throughput is not None:
        return round(saturation.approx_throughput)
    if saturation.lower_bound is not None:
        return round(saturation.lower_bound)
    print(
        f"AVISO: rampa de confirmação da seletividade {tier} não obteve S nem lower_bound "
        f"(gargalo do gerador) — usando fallback fixo de {LEGACY_HIGH_RATE_FALLBACK} req/s "
        "como nível de carga 'alto' dessa seletividade."
    )
    return LEGACY_HIGH_RATE_FALLBACK


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
    skip_dataset_load: bool = False,
) -> str:
    """Schema + massa de dados COMPLETA (mode="full" — não o subconjunto do
    oráculo do smoke test: load/zipf.js amostra de toda a população real) +
    fixture de contexto-por-seletividade, antes de qualquer medição — reusa
    a mesma construção de env vars e passos de schema/carga do smoke test
    (Fase 4), sem os gates de correção/smoke, que não são responsabilidade
    desta fase. Bind-monta `fixtures_mount` para `contexts_by_tier.json`
    sobreviver e ser lido depois por cada `docker run` de
    `load/run_battery.py`.

    `skip_dataset_load=True` pula schema+load_full_dataset.py inteiro —
    usado quando o disco já veio carregado de um snapshot
    (infra/scripts/seed_dataset_snapshots.py, main() em run_measurement_battery.py)
    e recarregar do zero desperdiçaria horas à toa. `export_contexts_by_tier.py`
    nunca depende do banco, então sempre roda.

    Quando a carga completa roda (`skip_dataset_load=False`), o comando
    inteiro é envolvido com `tee` para um log dentro de `fixtures_mount`
    (sobrevive ao --rm do container, fica no disco da VM loadgen) — sem
    isso, uma carga real (~horas em escala grande) fica muda até a sessão
    SSH inteira terminar: gcloud_ssh() usa capture_output=True, que só
    entrega stdout quando o processo remoto sai, então "ainda carregando"
    e "travou há 3 horas" ficam indistinguíveis vistos de fora — confirmado
    ao vivo com e1-scylla em us-east4. Uma segunda sessão SSH leve
    (cloud_smoke_test.tail_remote_file), concorrente com esta, consegue ler
    esse log a qualquer momento sem esperar o comando principal terminar.
    PYTHONUNBUFFERED=1 é o outro lado disso: sem ele, o próprio Python
    remoto bufferiza stdout inteiro (não está preso a um terminal), e nem
    o tee veria as linhas de progresso até o processo sair."""
    env_flags = build_storage_env_flags(cell_id, storage, database_ip, service_ip, postgres_password)
    env_flags = [*env_flags, f"DATASET_BUCKET={dataset_bucket}", "PYTHONUNBUFFERED=1"]
    steps = [] if skip_dataset_load else build_schema_and_fixture_steps(storage, mode="full")
    steps.append("python load/export_contexts_by_tier.py")
    inner = " && ".join(steps)
    if not skip_dataset_load:
        inner = f"set -o pipefail; ({inner}) 2>&1 | tee /app/load/fixtures/setup.log"

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
    return f"{_remote_pull_with_login(tools_image)} && {shlex.join(docker_argv)}"


def _remote_pull_with_login(image: str, retries: int = 20, sleep_s: int = 20) -> str:
    """Login + pull explícito, com retry generoso, imediatamente antes do
    `docker run` real — não confia no pre-pull feito pelo startup-script da
    VM no boot (infra/modules/loadgen/main.tf). Esse pre-pull roda em
    paralelo com a propagação da concessão roles/artifactregistry.reader
    recém-criada (o depends_on do Terraform garante que a concessão foi
    CRIADA antes da VM, não que já tenha PROPAGADO no backend de IAM do
    Google), e falha em silêncio: o loop `... || sleep N; done` do boot
    nunca verifica se alguma tentativa deu certo. Confirmado ao vivo em
    us-east4, duas vezes seguidas mesmo já com o fix de depends_on: o pull
    de boot nunca completou, e só descobrimos minutos depois, aqui, com
    "Unable to find image ... locally" seguido do mesmo erro de auth —
    exatamente o tipo de falha tardia e confusa que este login+pull
    dedicado, rodado no momento exato em que a imagem é necessária (não no
    boot, sem relação com o resto do que a VM está fazendo), evita."""
    registry_host = image.split("/", 1)[0]
    token_cmd = (
        "curl -sf -H 'Metadata-Flavor: Google' "
        "'http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token' "
        "| sed -n 's/.*\"access_token\": *\"\\([^\"]*\\)\".*/\\1/p'"
    )
    login_cmd = (
        f"export DOCKER_CONFIG=/tmp/.docker-setup && "
        f"{token_cmd} | docker login -u oauth2accesstoken --password-stdin https://{registry_host}"
    )
    # for ((...)) em vez de `seq`: não é garantido que `seq` exista na
    # imagem mínima do Container-Optimized OS, e o loop bash builtin não
    # depende de nenhum binário externo. `ok=1`/checagem no final: sem
    # isso, o loop "sempre dá certo" como statement bash (a última
    # iteração roda `sleep` ou `break`, ambos saem 0) mesmo se NENHUM pull
    # funcionar — exatamente o bug do loop equivalente no startup-script
    # (infra/modules/loadgen/main.tf), que deixa a falha real silenciosa
    # até aparecer, confusa, no `docker run` mais adiante.
    pull_loop = (
        f"ok=0; for ((i=0; i<{retries}; i++)); do "
        f"docker pull {image} && ok=1 && break || sleep {sleep_s}; done; "
        f'[ "$ok" = 1 ] || {{ echo "ERRO: falha ao puxar {image} apos {retries} tentativas" >&2; exit 1; }}'
    )
    return f"{login_cmd} && {pull_loop}"


def _duration_seconds(duration: str) -> int:
    """'0s'/'2m'/'3m' (o subconjunto de duração k6 que este orquestrador
    usa) em segundos — para converter taxa × duração em contagem esperada
    de requisições (`--expected-requests` de analysis/probe_report.py)."""
    unit, value = duration[-1], int(duration[:-1])
    if unit == "s":
        return value
    if unit == "m":
        return value * 60
    raise ValueError(f"duração k6 não suportada: {duration!r} (use Ns ou Nm)")


def build_remote_battery_command(
    cell_id: str,
    target_url: str,
    phase: str,
    rate: int,
    tier: str,
    repetition_index: int,
    tools_image: str,
    results_mount: str,
    fixtures_mount: str,
    timestamp: str,
    region: str | None = None,
    zone: str | None = None,
    results_bucket: str | None = None,
    *,
    user_count: int,
) -> str:
    """Monta `docker run ... load/run_battery.py ...` como argv +
    shlex.join, mesmo padrão de segurança de
    cloud_smoke_test.py:_docker_run_script (nunca concatenar strings
    com aspas manualmente). Bind-monta `results_mount` porque o container é
    `--rm` — sem isso, `results/` some quando ele sai — e `fixtures_mount`
    (somente leitura) para reusar o `contexts_by_tier.json` que o setup já
    gerou, sem reexportar em toda combinação de carga/seletividade.
    `--timestamp` fixo faz todas as combinações desta execução caírem no
    mesmo results/<cell>/<phase>/<timestamp>/, no mesmo diretório onde
    _write_saturation_json também escreve saturation.json.

    `--repetitions 1 --repetition-index N`, não `--repetitions N`: cada
    chamada roda EXATAMENTE uma repetição — o chamador (main()) faz uma
    sessão SSH por repetição via gcloud_ssh_with_retry, em vez de encadear
    todas numa sessão só (uma queda de conexão no meio perdia a combinação
    inteira, confirmado ao vivo 2x contra e3-valkey)."""
    battery_argv = [
        "load/run_battery.py",
        "--cells",
        cell_id,
        "--target-url",
        target_url,
        "--phase",
        phase,
        "--repetitions",
        "1",
        "--repetition-index",
        str(repetition_index),
        "--rate",
        str(rate),
        "--selectivity-tier",
        tier,
        "--timestamp",
        timestamp,
        # Sem isso load/run_battery.py recusa rodar (e com razão): o Zipf do
        # k6 precisa amostrar a base carregada INTEIRA, não o default
        # dev-scale de load/zipf.js — ver MAIN_MEASUREMENT_USER_COUNT.
        "--user-count",
        str(user_count),
    ]
    # Região/zona vão para o manifest.json de cada repetição (load/run_battery.py):
    # sem elas, um resultado arquivado não diz em que região foi medido — e a
    # região determina os preços que alimentam o modelo de custo.
    if region:
        battery_argv += ["--region", region]
    if zone:
        battery_argv += ["--zone", zone]
    if results_bucket:
        battery_argv += ["--results-bucket", results_bucket]

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


def _probe_stat_path(remote_subdir: str, when: str) -> str:
    return f"/app/results/{remote_subdir}/{when}_stat.txt"


def build_remote_probe_rep_command(
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
    rep: int,
    capture_before_stat: bool,
    capture_after_stat: bool,
    results_bucket: str | None = None,
    *,
    user_count: int,
) -> str:
    """Uma repetição de uma sondagem da busca de saturação
    (load/saturation.py) — antes, TODAS as repetições de uma sondagem
    rodavam encadeadas numa única sessão SSH (`&&` num só `docker run`); uma
    queda de conexão no meio perdia a sondagem inteira, confirmado ao vivo
    2x contra e3-valkey. Agora cada repetição é sua própria sessão
    (chamador: make_probe_fn, via gcloud_ssh_with_retry) — perder uma
    tentativa custa só ela, não as repetições anteriores (já persistidas em
    /app/results, que sobrevive entre invocações `docker run --rm`).

    capture_before_stat/capture_after_stat: só a 1ª e a última repetição leem
    /proc/stat, gravando num arquivo (não variável de ambiente — não
    sobrevive entre sessões SSH separadas) para
    build_remote_probe_aggregate_command ler depois e calcular a CPU do
    gerador ao longo da sondagem inteira."""
    rep_dir = f"/app/results/{remote_subdir}/rep{rep}"
    json_out = f"{rep_dir}/k6-raw.json"
    steps: list[str] = []
    # mkdir -p vem antes de tudo, inclusive da captura de before_stat: ele
    # também cria remote_subdir (pai de rep_dir), e o arquivo de before_stat
    # mora em remote_subdir — sem isto a repetição 0 falha com "No such file
    # or directory" ao gravar before_stat.txt num diretório que ainda não
    # existe. Confirmado ao vivo contra e3-valkey (4ª tentativa): as 3
    # tentativas de gcloud_ssh_with_retry falharam de forma idêntica e
    # determinística, não por queda de conexão.
    steps.append(shlex.join(["mkdir", "-p", rep_dir]))
    if capture_before_stat:
        steps.append(f"cat /proc/stat | head -1 > {_probe_stat_path(remote_subdir, 'before')}")
    # k6 não cria o diretório de --out sozinho (diferente de
    # load/run_battery.py:run_k6, que faz out_dir.mkdir(parents=True) em
    # Python antes de chamar o k6) — confirmado ao vivo: "open
    # .../k6-raw.json: no such file or directory" na 1ª sondagem de
    # saturação real, um caminho _saturation/<cell>/<probe>/rep<N>/ nunca
    # criado antes.
    k6_argv = build_probe_k6_cmd(
        json_out, cell_id, target_url, rate, tier, warmup, measure, user_count=user_count
    )
    steps.append(shlex.join(str(a) for a in k6_argv))
    if results_bucket:
        # Sobe k6-raw.json e apaga a cópia local logo em seguida — sem isto
        # o disco da loadgen acumula um k6-raw.json por repetição de CADA
        # sondagem até o fim da busca inteira. Confirmado ao vivo: ~5GB por
        # repetição em patamares de carga alta encheu o disco de 100GB e
        # derrubou e3-postgres com "no space left on device" na rampa fina
        # de confirmação.
        blob_name = f"{remote_subdir}/rep{rep}/k6-raw.json"
        steps.append(
            shlex.join(["python", "load/upload_one_file.py", json_out, results_bucket, blob_name])
        )
        steps.append(shlex.join(["rm", "-f", json_out]))
    if capture_after_stat:
        steps.append(f"cat /proc/stat | head -1 > {_probe_stat_path(remote_subdir, 'after')}")
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


def build_remote_probe_aggregate_command(
    remote_subdir: str,
    repetitions: int,
    rate: int,
    measure: str,
    tools_image: str,
    results_mount: str,
    fixtures_mount: str,
    decision_statistic: str = "pooled",
    ignore_latency_slo: bool = False,
) -> str:
    """Consolida as `repetitions` já rodadas por build_remote_probe_rep_command
    com analysis/probe_report.py, na sessão SSH final e separada da
    sondagem — evita expor polars ao host (só o resultado de uma linha
    `PROBE_RESULT ...` volta via stdout do SSH, capturado por
    _parse_probe_result). Lê as duas leituras de /proc/stat que as
    repetições extremas gravaram em arquivo (build_remote_probe_rep_command:
    capture_before_stat/capture_after_stat) — variável de ambiente não
    sobrevive entre sessões SSH separadas, arquivo em /app/results sim."""
    ndjson_paths = [
        f"/app/results/{remote_subdir}/rep{rep}/requests.ndjson" for rep in range(repetitions)
    ]
    # taxa × janela de medição × repetições: o que o constant-arrival-rate
    # DEVERIA ter emitido nos cenários 'probe' (o warmup fica de fora — a
    # coleta filtra por scenario='probe'). Um déficit além do limiar de
    # analysis/collect.py:MIN_OFFERED_RATIO vira violated_slo=True no
    # veredito (docs/DESIGN.md, "Vazão ofertada verificada, não presumida").
    expected_requests = rate * _duration_seconds(measure) * repetitions
    # BEFORE_STAT/AFTER_STAT como atribuições próprias (separadas por &&, não
    # no mesmo prefixo de comando do python): mesmo padrão já usado antes de
    # dividir esta função — evita depender de expansão sequencial dentro de
    # uma única lista de atribuições-prefixo, que não é garantida.
    # --decision-statistic sempre explícito (nunca depende do default do
    # lado do container) — o comando remoto fica autodocumentado no log da
    # sessão SSH, sem precisar cruzar com a versão do container pra saber
    # qual estatística decidiu o veredito.
    # --ignore-latency-slo só quando o chamador pedir (default False, nunca
    # usado pela bateria principal) — ver analysis/probe_report.py:
    # violated_slo() para o motivo (campanha de estresse quer o teto real do
    # BANCO, não a SLO de latência do cliente).
    inner = (
        f'BEFORE_STAT="$(cat {_probe_stat_path(remote_subdir, "before")})" && '
        f'AFTER_STAT="$(cat {_probe_stat_path(remote_subdir, "after")})" && '
        'GENERATOR_CPU_STAT_BEFORE="$BEFORE_STAT" GENERATOR_CPU_STAT_AFTER="$AFTER_STAT" '
        "python analysis/probe_report.py "
        f"--expected-requests {expected_requests} "
        f"--decision-statistic {decision_statistic} "
        + ("--ignore-latency-slo " if ignore_latency_slo else "")
        + shlex.join(ndjson_paths)
    )

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


def build_remote_stat_capture_command(
    remote_subdir: str, when: str, tools_image: str, results_mount: str, fixtures_mount: str
) -> str:
    """Captura /proc/stat isolada, para quando o gerador de CPU precisa ser
    lido ANTES/DEPOIS de um bloco de repetições que não é uma sondagem
    própria (build_remote_probe_rep_command já faz isso, mas só dentro do
    fluxo de sondagem) — usada por build_final_level_confirmation_commands
    para bracketar as repetições arquivadas do patamar final com o mesmo
    par before/after_stat.txt que build_remote_probe_aggregate_command
    espera. `mkdir -p` primeiro: pode rodar antes de qualquer repetição
    real ter criado remote_subdir."""
    inner = (
        shlex.join(["mkdir", "-p", f"/app/results/{remote_subdir}"])
        + f" && cat /proc/stat | head -1 > {_probe_stat_path(remote_subdir, when)}"
    )
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


def build_final_level_confirmation_commands(
    cell_id: str,
    target_url: str,
    phase: str,
    rate: int,
    tier: str,
    timestamp: str,
    tools_image: str,
    results_mount: str,
    fixtures_mount: str,
    measure: str,
    decision_statistic: str,
    repetitions: int,
    region: str | None = None,
    zone: str | None = None,
    results_bucket: str | None = None,
    *,
    user_count: int,
) -> tuple[list[str], str]:
    """Confirma o patamar aprovado pela busca de saturação RODANDO-O como se
    fosse a combinação "alta" da bateria de carga fixa (build_remote_battery_command,
    mesmo formato arquivado — com manifest.json — que analysis/report.py lê),
    em vez da sondagem leve de load/saturation.py:_confirm_final_level (que
    grava só em _saturation/, sem manifest.json, nunca visto pelo
    relatório). Elimina a duplicação de medir o patamar final duas vezes —
    uma pela confirmação (auditoria descartável) e outra pela bateria fixa
    (dado arquivado) — usando as MESMAS `repetitions` sondagens para as duas
    coisas: dado arquivado (as respostas de build_remote_battery_command) e
    trilha de dispersão (per_rep_p99_ms/per_rep_violated_slo do agregado via
    --decision-statistic median-per-repetition). Combinado com
    --only-saturation (que já pula a bateria de carga fixa inteira) e o
    reaproveitamento dos níveis 100/1000 já medidos na campanha original,
    fica só 1 conjunto de repetições no patamar alto, não 3.

    Devolve (comandos_preparatórios, comando_agregado): o chamador roda os
    primeiros em sequência (captura de CPU antes, as `repetitions`
    repetições arquivadas, captura de CPU depois) e por último o agregado,
    lendo o veredito da última linha PROBE_RESULT do stdout."""
    remote_subdir = f"{cell_id}/{phase}/{timestamp}/{rate}-{tier}"
    prep_commands = [
        build_remote_stat_capture_command(remote_subdir, "before", tools_image, results_mount, fixtures_mount)
    ]
    for rep in range(repetitions):
        prep_commands.append(
            build_remote_battery_command(
                cell_id,
                target_url,
                phase,
                rate,
                tier,
                rep,
                tools_image,
                results_mount,
                fixtures_mount,
                timestamp,
                region=region,
                zone=zone,
                results_bucket=results_bucket,
                user_count=user_count,
            )
        )
    prep_commands.append(
        build_remote_stat_capture_command(remote_subdir, "after", tools_image, results_mount, fixtures_mount)
    )
    aggregate_command = build_remote_probe_aggregate_command(
        remote_subdir,
        repetitions,
        rate,
        measure,
        tools_image,
        results_mount,
        fixtures_mount,
        decision_statistic=decision_statistic,
    )
    return prep_commands, aggregate_command


def build_remote_upload_command(
    remote_dir: str,
    results_mount: str,
    results_bucket: str,
    prefix: str,
    tools_image: str,
) -> str:
    """Sobe `remote_dir` (caminho DENTRO do container, montado a partir de
    `results_mount` na VM) para `gs://results_bucket/prefix/` — substitui o
    par sync_results_from_loadgen (scp) + upload_results_to_bucket (do host)
    que existiam antes. `--network host`: obrigatório, é o que faz
    `load/upload_results.py`'s `storage.Client()` enxergar o metadata server
    da VM pra ADC (mesmo padrão de harness/fixtures.py:
    ensure_full_dataset_downloaded — ver docstring de load/upload_results.py
    para o histórico completo do porquê desta mudança)."""
    docker_argv = [
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "-v",
        f"{results_mount}:/app/results",
        "--entrypoint",
        "python",
        tools_image,
        "load/upload_results.py",
        remote_dir,
        results_bucket,
        prefix,
    ]
    return shlex.join(docker_argv)


def _parse_optional_float(token: str | None) -> float | None:
    """analysis/probe_report.py imprime `p99=None`/`error_rate=None` (texto
    literal do Python) quando build_summary não parseou nenhuma requisição
    — ver analysis/collect.py:build_summary, ramo latencies_df.is_empty()."""
    if token is None or token == "None":
        return None
    return float(token)


def _parse_optional_float_list(token: str | None) -> list[float] | None:
    """analysis/probe_report.py só imprime per_rep_p99_ms=.../per_rep_violated=
    no modo --decision-statistic median-per-repetition — None quando o
    token está ausente (modo pooled, ou saída de probe_report.py anterior
    a este par de campos)."""
    if token is None:
        return None
    return [float(v) for v in token.split(",")]


def _parse_optional_bool_list(token: str | None) -> list[bool] | None:
    if token is None:
        return None
    return [v == "True" for v in token.split(",")]


@dataclass(frozen=True)
class _ProbeVerdict:
    violated_slo: bool
    p99_ms: float | None
    error_rate: float | None
    generator_cpu_percent: float
    # None em saídas de probe_report.py anteriores a --expected-requests
    # (o token offered_ratio= não existia na linha PROBE_RESULT).
    offered_ratio: float | None = None
    # Trilha de auditoria de --decision-statistic median-per-repetition —
    # None no modo pooled (default) ou em saídas antigas sem esses tokens.
    per_rep_p99_ms: list[float] | None = None
    per_rep_violated_slo: list[bool] | None = None


def _parse_probe_result(stdout: str) -> _ProbeVerdict:
    """Lê a linha `PROBE_RESULT violated_slo=... p99=... error_rate=...
    request_count=... generator_cpu_percent=...` que analysis/probe_report.py
    imprime dentro do container remoto — um processo só decide o veredito
    inteiro da sondagem, incluída a CPU do gerador (lida de /proc/stat pelo
    wrapper bash de build_remote_probe_command, repassada por variável de
    ambiente — nunca consulta o Cloud Monitoring para este portão, ver
    docstring de analysis/probe_report.py:_cpu_percent_from_stat)."""
    for line in stdout.splitlines():
        if line.startswith("PROBE_RESULT"):
            tokens = dict(tok.split("=", 1) for tok in line.split()[1:])
            return _ProbeVerdict(
                violated_slo=tokens["violated_slo"] == "True",
                p99_ms=_parse_optional_float(tokens.get("p99")),
                error_rate=_parse_optional_float(tokens.get("error_rate")),
                generator_cpu_percent=float(tokens["generator_cpu_percent"]),
                offered_ratio=_parse_optional_float(tokens.get("offered_ratio")),
                per_rep_p99_ms=_parse_optional_float_list(tokens.get("per_rep_p99_ms")),
                per_rep_violated_slo=_parse_optional_bool_list(tokens.get("per_rep_violated")),
            )
    raise RuntimeError(
        f"analysis/probe_report.py não imprimiu PROBE_RESULT na saída remota:\n{stdout}"
    )


def snapshot_exists(project_id: str, snapshot_name: str) -> bool:
    """Confirma se um snapshot de dataset já semeado existe
    (infra/scripts/seed_dataset_snapshots.py) — evita passar
    `data_disk_snapshot` pra um nome que não existe (o `terraform apply`
    falharia tarde, já tendo cobrado pela VM). Nunca levanta — ausência é
    o caminho normal (banco nunca semeado, ou é valkey, que não usa
    disco persistente)."""
    cmd = ["gcloud", "compute", "snapshots", "describe", snapshot_name, f"--project={project_id}"]
    print(f"+ {' '.join(cmd)}")
    result = subprocess.run(
        _resolve_cmd(cmd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return result.returncode == 0


def verify_otel_pipeline(
    project_id: str,
    instance_by_component: dict[str, str],
    wait_seconds: int = 60,
) -> bool:
    """Confirma que o coletor OpenTelemetry das 3 VMs (infra/modules/
    {database,service,loadgen}/main.tf) está de fato exportando métricas
    para o Cloud Monitoring, antes de comprometer horas de VM numa triagem
    inteira — README.md/docs/DESIGN.md sinalizam essa peça como a menos
    testada do projeto (Ops Agent oficial do Google não roda em COS, sem
    gerenciador de pacotes). Pensado para rodar só na primeira célula da
    triagem (--verify-otel): o módulo Terraform é idêntico nas 14, então
    um OK aqui vale para todas."""
    from analysis.resources import GCPMonitoringCollector

    started_at = datetime.now(timezone.utc)
    # Três tentativas: `compute.googleapis.com/*` (métrica padrão do Compute
    # Engine) costuma ficar consultável rápido, mas `workload.googleapis.com/
    # system.memory.usage` (vem do NOSSO coletor OTel — um salto a mais:
    # hostmetrics -> exporter googlecloud -> ingestão do Cloud Monitoring)
    # demonstrou ao vivo precisar de mais que os ~300s (60+240) que 2
    # tentativas davam — CPU/rede já passavam nesse tempo, só memória ainda
    # não tinha ponto na janela. Cada tentativa usa uma janela desde
    # `started_at` (exceto a 1ª, mais estreita) para não perder a amostra
    # que uma tentativa anterior só não esperou tempo suficiente para ver.
    extra_waits = (wait_seconds, 240, 300)
    attempts = len(extra_waits)
    for attempt, extra_wait in enumerate(extra_waits, start=1):
        print(
            f"Aguardando {extra_wait}s para o coletor publicar uma amostra "
            f"(tentativa {attempt}/{attempts})..."
        )
        time.sleep(extra_wait)

        end_time = datetime.now(timezone.utc)
        start_time = started_at if attempt > 1 else end_time - timedelta(seconds=wait_seconds)
        collector = GCPMonitoringCollector(project_id, instance_by_component, start_time, end_time)
        try:
            samples = collector.collect()
        except Exception as exc:
            print(f"FALHOU (tentativa {attempt}/{attempts}): {exc}")
            continue

        for sample in samples:
            memory_available = (
                f"{sample.memory_available_mb:.0f}MB"
                if sample.memory_available_mb is not None
                else "n/a"
            )
            print(
                f"  {sample.component}: cpu={sample.cpu_percent:.1f}% "
                f"memory={sample.memory_mb:.0f}MB network={sample.network_mbps:.2f}Mbps "
                f"memory_available={memory_available}"
            )
        print("OK: as 3 VMs estão exportando CPU/memória/rede para o Cloud Monitoring.")
        return True

    print(
        "Diagnóstico: `gcloud compute ssh <instância> --zone=<zone> --project="
        f"{project_id} --tunnel-through-iap` e `docker logs tcc-otel-agent` em cada VM "
        "para ver o erro real — as duas tentativas automáticas já deram tempo de sobra para "
        "propagação normal de métrica; se ainda assim falhou, é mais provável ser o coletor "
        "de verdade quebrado do que atraso."
    )
    return False


def make_resource_collect_fn(
    project_id: str,
    instance_by_component: dict[str, str],
    window_seconds: int = RESOURCE_QUERY_WINDOW_SECONDS,
    ingestion_delay_seconds: int = RESOURCE_INGESTION_DELAY_SECONDS,
) -> Callable[[], list]:
    """Fecha sobre o contexto de uma célula e devolve uma função sem
    argumentos que consulta uma janela recente do Cloud Monitoring — usada
    por sample_resources_periodically. A janela termina `ingestion_delay_seconds`
    ANTES de "agora" (o ponto mais recente de uma métrica customizada
    normalmente ainda não foi ingerido no instante em que é gerado) e tem
    `window_seconds` de largura — bem maior que o intervalo de tick
    (RESOURCE_SAMPLE_INTERVAL_SECONDS), de propósito: `system.cpu.time` só
    ganha um ponto novo a cada ~60s (piso do Cloud Monitoring para métricas
    customizadas), então uma janela mais estreita que isso nunca contém os
    2 pontos necessários para calcular uma taxa de CPU — bug real
    encontrado ao vivo quando a janela era amarrada ao próprio intervalo de
    tick (5s), fazendo as 5 execuções da confirmação rodarem inteiras sem
    escrever uma amostra sequer."""
    from analysis.resources import GCPMonitoringCollector

    def collect_fn() -> list:
        end_time = datetime.now(timezone.utc) - timedelta(seconds=ingestion_delay_seconds)
        start_time = end_time - timedelta(seconds=window_seconds)
        collector = GCPMonitoringCollector(project_id, instance_by_component, start_time, end_time)
        return collector.collect()

    return collect_fn


def sample_resources_periodically(
    collect_fn: Callable[[], list],
    stop_event: threading.Event,
    samples_out: list,
    interval_seconds: int = RESOURCE_SAMPLE_INTERVAL_SECONDS,
) -> None:
    """Chama collect_fn() a cada interval_seconds até stop_event ser
    sinalizado, acumulando em samples_out — roda numa thread separada, em
    paralelo ao sweep/rampa de confirmação (docs/DESIGN.md, "Atribuição de
    gargalo"). interval_seconds é só o intervalo de VERIFICAÇÃO — a janela
    de dado em si é decidida por collect_fn (ver make_resource_collect_fn),
    não por este parâmetro. collect_fn isolado por injeção de dependência
    para este loop ser testável com um fake, sem precisar de Cloud
    Monitoring de verdade — uma falha isolada de coleta (rede, métrica
    ainda não disponível) não derruba o loop nem a medição em andamento."""
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
    results_bucket: str | None = None,
    decision_statistic: str = "pooled",
    *,
    user_count: int,
    run_timestamp: str,
) -> Callable[[int], ProbeResult]:
    """Fecha sobre o contexto de rede/infra de uma célula e devolve um
    probe_fn(rate) -> ProbeResult para load.saturation.run_saturation_search
    — cada chamada roda a sondagem remota via SSH e lê o veredito inteiro
    (SLO + p99/error_rate + CPU do gerador) de uma única linha PROBE_RESULT
    impressa por analysis/probe_report.py dentro do container (CPU vem de
    /proc/stat lido na própria VM, não do Cloud Monitoring).

    run_timestamp entra no path _saturation/ porque probe_id (label+contador+
    rate) é determinístico entre execuções — um retry após falha no meio da
    rampa gera o MESMO probe_id da tentativa anterior. Sem o timestamp, o
    upload do retry colide com o k6-raw.json já enviado pela tentativa
    falha (upload_one_file.py usa if_generation_match=0, então rejeita com
    412 em vez de sobrescrever silenciosamente) — confirmado ao vivo no
    retry de e3-valkey."""
    counter = itertools.count()

    def probe_fn(rate: int) -> ProbeResult:
        probe_id = f"{label}-{next(counter)}-{rate}"
        remote_subdir = f"_saturation/{cell_id}/{run_timestamp}/{probe_id}"

        for rep in range(repetitions):
            rep_cmd = build_remote_probe_rep_command(
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
                rep,
                capture_before_stat=(rep == 0),
                capture_after_stat=(rep == repetitions - 1),
                results_bucket=results_bucket,
                user_count=user_count,
            )
            gcloud_ssh_with_retry(loadgen_instance, zone, project_id, rep_cmd)

        aggregate_cmd = build_remote_probe_aggregate_command(
            remote_subdir,
            repetitions,
            rate,
            measure,
            tools_image,
            results_mount,
            fixtures_mount,
            decision_statistic=decision_statistic,
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
            per_rep_p99_ms=verdict.per_rep_p99_ms,
            per_rep_violated_slo=verdict.per_rep_violated_slo,
        )

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
            "censurada nessa dimensão (docs/DESIGN.md: tratada como empatada com outras censuradas "
            "e superior a qualquer não-censurada, nunca como o teto de verdade)."
        )
    else:
        print(f"{prefix}vazão de saturação aproximada: {saturation.approx_throughput:.0f} req/s.")

    # Bloco independente do veredito acima: "não medido" pode coexistir com
    # censurada ou com uma vazão aproximada perfeitamente boa.
    unmeasured = sum(1 for p in saturation.probes if p.generator_cpu_percent is None)
    if unmeasured:
        print(
            f"{prefix}ATENÇÃO: {unmeasured} de {len(saturation.probes)} sondagens ficaram sem "
            f"leitura de CPU do gerador — o portão de validade do docs/DESIGN.md (CPU < "
            f"{GENERATOR_CPU_THRESHOLD:.0f}%) NÃO pôde ser avaliado nelas. Não é o mesmo que "
            "gerador ocioso: este resultado não está validado nessa dimensão. Confira o coletor "
            "OTel do loadgen (--verify-otel) antes de usar esta vazão na dissertação."
        )


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
        "generator_cpu_unmeasured": saturation.generator_cpu_unmeasured,
        "probes": [
            {
                "rate": p.rate,
                "violated_slo": p.violated_slo,
                "generator_cpu_percent": p.generator_cpu_percent,
                "p99_ms": p.p99_ms,
                "error_rate": p.error_rate,
                # Auditoria: distingue "violou o SLO" de "o k6 nem conseguiu
                # ofertar o patamar" (docs/DESIGN.md, vazão ofertada).
                "offered_ratio": p.offered_ratio,
                # Auditoria de --decision-statistic median-per-repetition:
                # None no modo pooled (default) ou em sondagens antigas.
                "per_rep_p99_ms": p.per_rep_p99_ms,
                "per_rep_violated_slo": p.per_rep_violated_slo,
            }
            for p in saturation.probes
        ],
        # Repetições do patamar aprovado, separadas da trilha de busca: é
        # sobre elas que a análise calcula a dispersão de S (quantas violaram
        # o SLO no mesmo patamar, e como o p99 variou). Lista vazia em
        # execuções antigas ou em células censuradas.
        "final_level_probes": [
            {
                "rate": p.rate,
                "violated_slo": p.violated_slo,
                "generator_cpu_percent": p.generator_cpu_percent,
                "p99_ms": p.p99_ms,
                "error_rate": p.error_rate,
                "offered_ratio": p.offered_ratio,
                "per_rep_p99_ms": p.per_rep_p99_ms,
                "per_rep_violated_slo": p.per_rep_violated_slo,
            }
            for p in saturation.final_level_probes
        ],
    }
    (out_dir / filename).write_text(json.dumps(payload, indent=2))


def build_saturation_upload_cmd(
    cell_id: str, phase: str, timestamp: str, filename: str, results_bucket: str
) -> list[str]:
    local_path = Path("results") / cell_id / phase / timestamp / filename
    return [
        "gcloud",
        "storage",
        "cp",
        str(local_path),
        f"gs://{results_bucket}/{cell_id}/{phase}/{timestamp}/{filename}",
    ]


def _upload_saturation_json(
    cell_id: str, phase: str, timestamp: str, filename: str, results_bucket: str
) -> None:
    # _write_saturation_json escreve só no host orquestrador (nunca passa
    # pela VM loadgen, então results_upload_cmd/saturation_upload_cmd mais
    # abaixo não o alcançam) — mesma classe de lacuna que resources.csv tinha
    # antes do `gcloud storage cp` explícito já usado para ele. Sem isto, a
    # trilha de auditoria por repetição (per_rep_p99_ms/per_rep_violated_slo)
    # só sobrevive na máquina que rodou a medição.
    _run(build_saturation_upload_cmd(cell_id, phase, timestamp, filename, results_bucket))


def main(argv: list[str] | None = None) -> int:
    # Mesmo fix de cloud_smoke_test.py:main() — a codepage cp1252 do console
    # do Windows não representa vários caracteres que aparecem na saída de
    # k6 (o resumo padrão usa símbolos Unicode como checkmarks e barras de
    # threshold). Aqui o risco é maior ainda: esta bateria roda por horas
    # imprimindo saída de k6/docker/terraform repetidamente por célula.
    # line_buffering=True: sem isso, print() fica em buffer de bloco (~8KB)
    # quando stdout é redirecionado para um arquivo (não um terminal) — quem
    # acompanha o log via `tail -f` vê silêncio por dezenas de minutos
    # mesmo com o wait_for_container/wait_for_service_ready/sweep avançando
    # normalmente nas VMs remotas. Confirmado ao vivo: e3-postgres pareceu
    # travado por >1h só por causa disso (SSH direto nas 3 VMs mostrou tudo
    # saudável e o k6 já rodando) — mesma classe de problema que já custou
    # ~3h30 de confusão em e1-scylla (ver docstring de tail_remote_file em
    # cloud_smoke_test.py).
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

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
        "--user-count",
        type=int,
        default=MAIN_MEASUREMENT_USER_COUNT,
        help="base de usuários que o Zipf do k6 amostra (USER_COUNT em load/zipf.js). O "
        "default é a base real completa que build_remote_setup_command carrega "
        "(docs/DESIGN.md, U da medição principal); sobrescreva SÓ na varredura de "
        "escalabilidade, junto com a massa sintética correspondente.",
    )
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
        "--only-saturation",
        action="store_true",
        help="pula a bateria de carga fixa e roda SÓ a rampa de saturação — para re-medir S "
        "com a metodologia nova sem descartar as repetições de latência já coletadas (que são "
        "a parte cara). O saturation.json novo cai num diretório de timestamp próprio, sem "
        "rep*/; analysis/report.py o encontra varrendo a árvore, não derivando dos rep_dirs.",
    )
    parser.add_argument(
        "--tier",
        choices=SELECTIVITY_TIERS,
        default=None,
        help="restringe a rampa de confirmação e a bateria de carga fixa a UMA seletividade "
        "(default: as 3, comportamento atual) — para re-medir só uma delas sem gastar tempo/$ "
        "refazendo as outras duas. Só faz sentido com --phase confirmacao: a triagem já é fixa "
        "em seletividade 'medium' (TRIAGEM_TIER).",
    )
    parser.add_argument(
        "--decision-statistic",
        choices=("pooled", "median-per-repetition"),  # analysis/probe_report.py:DECISION_STATISTICS
        default="pooled",
        help="estatística de decisão do SLO em cada sondagem da busca de saturação (default "
        "'pooled', preserva o comportamento histórico — repassado a analysis/probe_report.py, "
        "ver lá para a explicação completa de 'median-per-repetition').",
    )
    parser.add_argument(
        "--saturation-min-step",
        type=int,
        default=None,
        help="faz a busca binária da rampa de confirmação parar por LARGURA de intervalo (req/s) "
        "em vez de por um número fixo de iterações — útil quando se quer um S com precisão-alvo. "
        "Default None preserva o comportamento antigo (BINARY_SEARCH_ITERATIONS iterações, "
        "load/saturation.py). Quando informado, o teto de iterações sobe para "
        "SATURATION_MIN_STEP_ITERATION_CEILING (rede de segurança, não o critério de parada).",
    )
    parser.add_argument(
        "--keep-infra",
        action="store_true",
        help="não roda terraform destroy no final (para investigar uma falha)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="pula a confirmação interativa 'sim' antes de apply/destroy (uso supervisionado, "
        "ex.: uma sessão automatizando várias células em sequência com aprovação já dada fora "
        "deste comando) — ainda imprime o aviso FATURÁVEL, só não bloqueia em input().",
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

    if args.tier is not None and args.phase == "triagem":
        print(
            "ERRO: --tier não se aplica a --phase triagem — a triagem já roda fixa em "
            f"seletividade {TRIAGEM_TIER!r} (TRIAGEM_TIER), não há 3 seletividades para "
            "restringir.",
            file=sys.stderr,
        )
        return 1

    # Escopo de seletividades desta execução — default None = --tier não
    # informado = as 3 (comportamento atual). Usado tanto no laço da rampa
    # de confirmação quanto em build_sweep, pra --tier restringir as duas
    # coisas junto (docs/DESIGN.md, re-medição de seletividade média).
    tiers_to_run = [args.tier] if args.tier is not None else SELECTIVITY_TIERS

    storage = storage_for_cell(args.cell)
    if args.phase == "triagem":
        sweep = shuffled_sweep(build_sweep(args.phase), args.seed)
        print(f"sweep embaralhado (seed={args.seed}, fase={args.phase}): {sweep}")
        billable_combo_count = len(sweep)
    else:
        # Confirmação: o sweep depende do nível "alto" de cada seletividade,
        # só conhecido depois das rampas de saturação (mais abaixo, antes
        # do laço da bateria) — a contagem em si (não os valores) já é fixa.
        sweep = None
        billable_combo_count = len(tiers_to_run) * (len(FIXED_LOAD_LEVELS) + 1)

    # Reuso de snapshot de disco (infra/scripts/seed_dataset_snapshots.py) —
    # a carga completa é idêntica entre todas as células de uma mesma
    # tecnologia de banco, então um snapshot "tcc-dataset-seed-<storage>"
    # semeado uma vez cobre as 3/4 células dessa tecnologia. Valkey fica de
    # fora: roda 100% em memória, sem disco persistente (README.md).
    snapshot_name = f"tcc-dataset-seed-{storage}"
    snapshot_found = storage != "valkey" and snapshot_exists(args.project_id, snapshot_name)
    if snapshot_found:
        print(
            f"snapshot '{snapshot_name}' encontrado — disco será restaurado dele, "
            "pulando a carga completa do dataset nesta execução."
        )
    elif storage == "valkey":
        print("storage=valkey não usa snapshot de disco (sem persistência) — carregando em memória.")
    else:
        print(
            f"nenhum snapshot '{snapshot_name}' encontrado para storage={storage} — carregando "
            "o dataset do zero nesta execução (rode infra/scripts/seed_dataset_snapshots.py "
            f"{storage} pra evitar essa espera da próxima vez)."
        )

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
            f"criar VMs reais (banco + serviço + loadgen) e rodar {billable_combo_count} "
            f"combinação(ões) de carga/seletividade x {args.repetitions} repetições, mais a "
            "busca de vazão de saturação — pode levar horas e cobra o tempo todo.",
            auto_approve=args.yes,
        )
        applied = True
        terraform(
            [
                "init",
                "-reconfigure",
                f"-backend-config=bucket={args.terraform_state_bucket}",
                f"-backend-config=prefix=cells/{args.cell}",
            ],
            cell=args.cell,
        )
        apply_vars = [
            "apply",
            "-auto-approve",
            f"-var=project_id={args.project_id}",
            f"-var=region={args.region}",
            f"-var=zone={args.zone}",
            f"-var=cell={args.cell}",
            f"-var=storage={storage}",
            f"-var=dataset_bucket={args.dataset_bucket}",
            f"-var=results_bucket={args.results_bucket}",
        ]
        if snapshot_found:
            apply_vars.append(f"-var=data_disk_snapshot={snapshot_name}")
        terraform(apply_vars, cell=args.cell)

        outputs = terraform_output_json(cell=args.cell)
        database_ip = outputs["database_internal_ip"]
        service_ip = outputs["service_internal_ip"]
        database_instance = f"tcc-{args.cell}-database"
        service_instance = f"tcc-{args.cell}-service"
        loadgen_instance = f"tcc-{args.cell}-loadgen"
        # IDs numéricos, não os nomes acima — confirmado ao vivo:
        # resource.labels.instance_id do Cloud Monitoring para métricas
        # gce_instance (ex.: compute.googleapis.com/instance/cpu/utilization)
        # é o ID numérico da VM, não o nome. Filtrar pelo nome nunca casava
        # nenhuma série (não era atraso de propagação, como se pensou antes
        # de checar isso). Nomes continuam usados para SSH/wait_for_container
        # acima — só as chamadas ao Cloud Monitoring abaixo usam os IDs.
        loadgen_instance_id = outputs["loadgen_instance_id"]

        wait_for_container(database_instance, args.zone, args.project_id, "tcc-database")
        wait_for_container(service_instance, args.zone, args.project_id, "tcc-service")

        instance_by_component = {
            "database": outputs["database_instance_id"],
            "service": outputs["service_instance_id"],
            "loadgen": loadgen_instance_id,
        }

        if args.verify_otel:
            print("\n--- verificação do coletor OpenTelemetry/Ops Agent ---")
            if not verify_otel_pipeline(args.project_id, instance_by_component):
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
            skip_dataset_load=snapshot_found,
        )
        gcloud_ssh(loadgen_instance, args.zone, args.project_id, setup_cmd)

        if not snapshot_found:
            # tcc-service sobe no boot da VM, antes do schema/carga acima
            # existir (setup_cmd roda depois, via este SSH) — sem reiniciar,
            # o catálogo item->contexto de E-1/E-3 (carregado uma única vez
            # no lifespan de startup, service/http_app.py) fica com o dado
            # (vazio) de antes desta carga, para sempre, sem erro nenhum. Só
            # quando skip_dataset_load (snapshot_found), o dado já está no
            # disco antes do banco sequer ficar pronto — nesse caso o
            # crash-loop-retry natural do tcc-service já resolve, sem
            # precisar de restart explícito. Ver restart_container.
            print("\n--- recarregar catálogo do serviço ---")
            restart_container(service_instance, args.zone, args.project_id, "tcc-service")
            wait_for_container(service_instance, args.zone, args.project_id, "tcc-service")

        # Depois do schema/carga, não antes: numa VM sem snapshot o banco só
        # passa a responder consulta de verdade quando as tabelas existem.
        # Sonda o caminho completo (gerador -> serviço -> banco) até vir 200,
        # para o warmup do k6 ser aquecimento real e não os primeiros ~90s de
        # HTTP 500 de um cluster ainda subindo — ver wait_for_service_ready.
        wait_for_service_ready(loadgen_instance, args.zone, args.project_id, service_ip)

        target_url = f"http://{service_ip}:8000/v1/recommendations"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        # Limpa os diretórios locais AGORA, antes de qualquer coisa desta
        # execução escrever neles — não logo antes do sync no final (era o
        # bug real: a rampa de saturação já tinha escrito saturation.json
        # aqui embaixo, ANTES do sync, e um rmtree feito só ali apagava o
        # próprio arquivo que esta mesma execução tinha acabado de gerar,
        # confirmado ao vivo em e1-scylla). Sem isso, dado de uma execução
        # anterior (outro dia, às vezes outra região) ficava acumulado e
        # era reenviado ao bucket junto com o novo — a VM remota é sempre
        # nova por célula, então só o lado local sobrevive entre execuções.
        shutil.rmtree(Path("results") / args.cell, ignore_errors=True)
        shutil.rmtree(Path("results") / "_saturation" / args.cell, ignore_errors=True)

        # Amostragem periódica de recursos (docs/DESIGN.md: "amostrar a cada 5
        # segundos nas três VMs") — só durante a confirmação; a rampa curta
        # da triagem é exploratória e nunca é arquivada, o mesmo vale para o
        # uso de recursos dela.
        resource_samples: list = []
        stop_sampling = threading.Event()
        sampling_thread = None
        if args.phase == "confirmacao":
            collect_fn = make_resource_collect_fn(args.project_id, instance_by_component)
            sampling_thread = threading.Thread(
                target=sample_resources_periodically,
                args=(collect_fn, stop_sampling, resource_samples),
                daemon=True,
            )
            sampling_thread.start()

            # As rampas de saturação (uma por seletividade em tiers_to_run,
            # as 3 por default ou só 1 com --tier) rodam ANTES da bateria de
            # carga fixa — o nível "alto" dela (build_sweep) vem diretamente
            # daqui (docs/DESIGN.md, "Rampa de confirmação").
            high_rate_by_tier: dict[str, int] = {}
            for tier in tiers_to_run:
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
                    results_bucket=args.results_bucket,
                    decision_statistic=args.decision_statistic,
                    user_count=args.user_count,
                    run_timestamp=timestamp,
                )
                saturation = run_saturation_search(
                    probe_fn,
                    start_rate=int(args.saturation_start),
                    step_mode="fine",
                    # confirm_repetitions=0: a confirmação leve de
                    # load/saturation.py:_confirm_final_level (grava só em
                    # _saturation/, sem manifest.json) foi substituída pelo
                    # bloco abaixo, que roda o patamar aprovado no formato
                    # arquivado da bateria de carga fixa — as mesmas
                    # repetições servem de dado real (para o relatório) E de
                    # trilha de dispersão, em vez de medir o patamar alto
                    # duas vezes.
                    confirm_repetitions=0,
                    binary_search_min_step=args.saturation_min_step,
                    binary_search_iterations=(
                        SATURATION_MIN_STEP_ITERATION_CEILING
                        if args.saturation_min_step is not None
                        else BINARY_SEARCH_ITERATIONS
                    ),
                )
                _report_saturation(saturation, label=tier)
                if (
                    not saturation.censored
                    and not saturation.loadgen_bottleneck
                    and saturation.approx_throughput is not None
                ):
                    approved_rate = round(saturation.approx_throughput)
                    print(
                        f"\n--- confirmando patamar aprovado ({approved_rate} req/s, {tier}) "
                        "com dado arquivado (serve de bateria de carga 'alta' também) ---"
                    )
                    prep_commands, aggregate_command = build_final_level_confirmation_commands(
                        args.cell,
                        target_url,
                        args.phase,
                        approved_rate,
                        tier,
                        timestamp,
                        tools_image,
                        RESULTS_MOUNT,
                        FIXTURES_MOUNT,
                        CONFIRMATION_MEASURE,
                        args.decision_statistic,
                        CONFIRMATION_REPETITIONS,
                        region=args.region,
                        zone=args.zone,
                        results_bucket=args.results_bucket,
                        user_count=args.user_count,
                    )
                    for cmd in prep_commands:
                        gcloud_ssh_with_retry(loadgen_instance, args.zone, args.project_id, cmd)
                    agg_result = gcloud_ssh_with_retry(
                        loadgen_instance, args.zone, args.project_id, aggregate_command
                    )
                    verdict = _parse_probe_result(agg_result.stdout)
                    final_probe = ProbeResult(
                        rate=approved_rate,
                        violated_slo=verdict.violated_slo,
                        generator_cpu_percent=verdict.generator_cpu_percent,
                        p99_ms=verdict.p99_ms,
                        error_rate=verdict.error_rate,
                        offered_ratio=verdict.offered_ratio,
                        per_rep_p99_ms=verdict.per_rep_p99_ms,
                        per_rep_violated_slo=verdict.per_rep_violated_slo,
                    )
                    saturation = replace(saturation, final_level_probes=[final_probe])
                saturation_filename = f"saturation_{tier}.json"
                _write_saturation_json(
                    saturation, args.cell, args.phase, timestamp, filename=saturation_filename
                )
                _upload_saturation_json(
                    args.cell, args.phase, timestamp, saturation_filename, args.results_bucket
                )
                high_rate_by_tier[tier] = _high_rate_from_saturation(saturation, tier)

            sweep = shuffled_sweep(
                build_sweep(args.phase, high_rate_by_tier, tiers=tiers_to_run), args.seed
            )
            print(f"sweep embaralhado (seed={args.seed}, fase={args.phase}): {sweep}")

        if args.only_saturation:
            print(
                "\n--- --only-saturation: bateria de carga fixa PULADA "
                "(as repetições de latência já coletadas são preservadas) ---"
            )
        for i, (rate, tier) in enumerate([] if args.only_saturation else sweep, start=1):
            print(f"\n--- combinação {i}/{len(sweep)}: rate={rate} tier={tier} ---")
            # 1 sessão SSH por repetição, não todas encadeadas numa só — uma
            # queda de conexão no meio perdia a combinação inteira
            # (confirmado ao vivo 2x contra e3-valkey); agora custa, no pior
            # caso, a repetição atual (gcloud_ssh_with_retry já tenta de
            # novo antes de desistir).
            for rep in range(args.repetitions):
                print(f"  repetição {rep + 1}/{args.repetitions}")
                remote_cmd = build_remote_battery_command(
                    args.cell,
                    target_url,
                    args.phase,
                    rate,
                    tier,
                    rep,
                    tools_image,
                    RESULTS_MOUNT,
                    FIXTURES_MOUNT,
                    timestamp,
                    region=args.region,
                    zone=args.zone,
                    results_bucket=args.results_bucket,
                    user_count=args.user_count,
                )
                # print(result.stdout): sem isso, o resultado desta repetição
                # fica completamente mudo no log — confirmado ao vivo: um
                # crash posterior (na sondagem de saturação) levou o destroy
                # a rodar sem nunca ter sincronizado os resultados desta
                # combinação, e não havia NENHUM indício no log de como as
                # repetições tinham se saído antes de perdê-las com a VM.
                result = gcloud_ssh_with_retry(loadgen_instance, args.zone, args.project_id, remote_cmd)
                print(result.stdout)

        if args.phase == "triagem":
            print("\n--- rampa curta de saturação (exploratória, docs/DESIGN.md) ---")
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
                results_bucket=args.results_bucket,
                # repetitions=1 torna "median-per-repetition" e "pooled"
                # numericamente idênticos (mediana/min de 1 valor = o
                # próprio valor) — seguro repassar uniformemente, sem
                # precisar restringir --decision-statistic a --phase
                # confirmacao.
                decision_statistic=args.decision_statistic,
                user_count=args.user_count,
                run_timestamp=timestamp,
            )
            saturation = run_saturation_search(
                probe_fn,
                step_mode="fine",
                step=SHORT_RAMP_STEP,
                confirm_repetitions=SHORT_RAMP_CONFIRM_REPETITIONS,
            )
            _report_saturation(saturation)
            _write_saturation_json(saturation, args.cell, args.phase, timestamp)
            _upload_saturation_json(
                args.cell, args.phase, timestamp, "saturation.json", args.results_bucket
            )

        if sampling_thread is not None:
            stop_sampling.set()
            sampling_thread.join(timeout=RESOURCE_SAMPLE_INTERVAL_SECONDS + 10)
            if resource_samples:
                from analysis.resources import classify_bottleneck, write_resources_csv

                resources_path = Path("results") / args.cell / args.phase / timestamp / "resources.csv"
                write_resources_csv(resource_samples, resources_path)
                print(f"\n{resources_path}: {len(resource_samples)} amostras de recursos escritas.")
                # resources.csv nunca existiu na VM loadgen — GCPMonitoringCollector
                # roda do HOST, direto contra a API do Cloud Monitoring (ver
                # docstring do módulo). Por isso é o único arquivo que ainda sobe
                # via `gcloud storage cp` (host -> bucket): nunca passou pelo hop
                # scp (loadgen -> host) que era a causa real da falha em
                # e3-postgres, então não precisa da mudança de load/upload_results.py.
                _run(
                    [
                        "gcloud",
                        "storage",
                        "cp",
                        str(resources_path),
                        f"gs://{args.results_bucket}/{args.cell}/{args.phase}/{timestamp}/resources.csv",
                    ]
                )
                try:
                    bottleneck = classify_bottleneck(
                        resource_samples, memory_ceiling_mb=DEFAULT_MEMORY_MB_BY_COMPONENT
                    )
                    print(f"Gargalo dominante ao longo da confirmação: {bottleneck}.")
                except ValueError:
                    pass
            else:
                print("\nAVISO: nenhuma amostra de recursos coletada — resources.csv não foi escrito.")

        # Upload direto da VM loadgen pro bucket (load/upload_results.py, via
        # ADC/metadata server) — substitui o par scp (loadgen -> host) +
        # `gcloud storage cp` (host -> bucket) de antes. Motivo: no Windows,
        # `gcloud compute scp` roda sobre `pscp`/`plink` (PuTTY), cujo SFTP é
        # menos robusto sobre uma transferência grande através do túnel IAP —
        # confirmado ao vivo abortando a meio de um k6-raw.json de ~35MB em
        # e3-postgres, deixando VMs órfãs cobrando (ver docstring de
        # load/upload_results.py para o histórico completo).
        if args.phase == "confirmacao":
            # Diferente da rampa curta (exploratória, nunca arquivada), a de
            # confirmação exige "saída com distribuição completa" — as
            # sondagens brutas vão para <bucket>/_saturation/<cell>/, fora do
            # namespace <bucket>/<cell>/<phase>/ que analysis/report.py varre
            # (nunca entra por engano numa tabela de medição comum).
            saturation_upload_cmd = build_remote_upload_command(
                f"/app/results/_saturation/{args.cell}",
                RESULTS_MOUNT,
                args.results_bucket,
                f"_saturation/{args.cell}",
                tools_image,
            )
            gcloud_ssh(loadgen_instance, args.zone, args.project_id, saturation_upload_cmd)

        results_upload_cmd = build_remote_upload_command(
            f"/app/results/{args.cell}",
            RESULTS_MOUNT,
            args.results_bucket,
            args.cell,
            tools_image,
        )
        gcloud_ssh(loadgen_instance, args.zone, args.project_id, results_upload_cmd)

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
                f"terraform destroy da célula '{args.cell}' — é isso que PARA a cobrança.",
                auto_approve=args.yes,
            )
            # Reminta o token: o token impersonado dura só ~1h (fetch_terraform_
            # access_token), e uma bateria real (5 repetições x 7min de k6 +
            # busca de saturação) pode facilmente ultrapassar isso — confirmado
            # ao vivo: o destroy final falhou com "Error 401: invalid
            # authentication credentials" em VÁRIOS recursos, deixando VMs
            # presas cobrando até uma intervenção manual. Sem isso, exatamente
            # o cenário que este script deveria evitar (custo esquecido rodando)
            # acontecia justo na etapa que deveria parar a cobrança.
            print("Remintando token de acesso antes do destroy (o anterior pode ter expirado)...")
            os.environ["GOOGLE_OAUTH_ACCESS_TOKEN"] = fetch_terraform_access_token(args.project_id)
            destroy_vars = [
                "destroy",
                "-auto-approve",
                f"-var=project_id={args.project_id}",
                f"-var=region={args.region}",
                f"-var=zone={args.zone}",
                f"-var=cell={args.cell}",
                f"-var=storage={storage}",
                f"-var=dataset_bucket={args.dataset_bucket}",
                f"-var=results_bucket={args.results_bucket}",
            ]
            if snapshot_found:
                destroy_vars.append(f"-var=data_disk_snapshot={snapshot_name}")
            terraform(destroy_vars, cell=args.cell)

    return 0


if __name__ == "__main__":
    sys.exit(main())
