"""Bateria de execuções de load/scenarios.js — 5 repetições por célula, em
ordem aleatorizada (docs/DESIGN.md, "Protocolo de medição": "5 repetições por
célula, em ordem aleatorizada"; ver o plano em
implementacao-md-com-base-nos-staged-snowflake.md, Etapa 7). A seed usada
para embaralhar fica logada no manifesto de cada execução, para
reprodutibilidade — nunca correr as células na ordem em que os arquivos
cells/*.yaml aparecem no disco, o que enviesaria efeitos de ordem (cache do
SO ainda quente, throttling térmico acumulado) sempre para a mesma célula.

`cells/*.yaml` (exceto `_defaults.yaml`) já É a lista de células viáveis —
as 2 combinações arquiteturalmente inviáveis (E-4/Scylla, E-3/OpenSearch)
nunca ganharam arquivo (ver core/registry.py,
tests/acceptance/test_infeasible_cells_fail_at_startup.py). Não há outra
checagem de viabilidade a duplicar aqui.

Uso local (SMOKE apenas — docs/DESIGN.md proíbe medir latência localmente):
    docker compose run --rm --entrypoint python tools load/run_battery.py \\
        --cells e1-postgres --target-url http://service:8000/v1/recommendations \\
        --repetitions 1 --rate 10 --smoke

Uso real (nuvem — infra/, Fase 5): cada célula roda contra sua própria VM
de serviço; --targets aponta um JSON {cell_id: target_url} produzido a
partir dos outputs `service_internal_ip` de infra/envs/experiment. Quem
invoca isso de fato é infra/scripts/run_measurement_battery.py, um combo
(rate, selectivity_tier) por vez, via SSH na VM `loadgen`.

A vazão de saturação (docs/DESIGN.md, "Protocolo de medição") não é uma
"bateria" no sentido deste arquivo (repetições × células) — é uma busca
adaptativa de um único patamar por vez, orquestrada por
load/saturation.py, que usa build_probe_k6_cmd() abaixo em vez de
build_k6_cmd()/run_k6().
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

CELLS_DIR = Path("cells")
# .as_posix() nos usos abaixo, nunca str(): este processo às vezes roda no
# host (Windows, via run_measurement_battery.py:build_probe_k6_cmd, montado
# localmente e enviado por SSH como string) e às vezes dentro do container
# remoto (Linux, quando este próprio arquivo roda via `python
# load/run_battery.py`) — str(Path(...)) usa o separador nativo do SO onde
# roda, e o k6 do lado de lá é sempre Linux. Confirmado ao vivo: a sondagem
# de saturação (montada no host Windows) quebrava com "load\scenarios.js"
# não encontrado, enquanto a combinação principal (montada dentro do
# container Linux) sempre funcionou — mesma constante, dois separadores.
SCENARIOS_SCRIPT = Path("load/scenarios.js")
RESULTS_DIR = Path("results")


def list_viable_cell_ids(cells_dir: Path = CELLS_DIR) -> list[str]:
    return sorted(p.stem for p in cells_dir.glob("*.yaml") if p.stem != "_defaults")


def shuffled_cell_order(cell_ids: list[str], seed: int) -> list[str]:
    order = list(cell_ids)
    random.Random(seed).shuffle(order)
    return order


def build_run_plan(cell_ids: list[str], repetitions: int) -> list[tuple[str, int]]:
    """(cell_id, repetition_index) na ordem em que devem rodar: todas as
    repetições de uma célula antes de passar para a próxima — a
    aleatorização já está na ordem de `cell_ids`, repetições dentro da
    mesma célula não precisam de nova ordem embaralhada."""
    return [(cell_id, rep) for cell_id in cell_ids for rep in range(repetitions)]


def combo_out_dir(
    results_dir: Path, cell_id: str, phase: str, timestamp: str, rate: int, tier: str, repetition: int
) -> Path:
    """results/<cell>/<phase>/<timestamp>/[<rate>-<tier>/]rep<N> —
    docs/ARCHITECTURE.md, "Coleta de resultados". Só a confirmação recebe o
    segmento <rate>-<tier>/: ela varre 9 combinações (RATES × SELECTIVITY_TIERS
    em infra/scripts/run_measurement_battery.py) que, sem isso, cairiam todas
    nos mesmos 5 diretórios rep0..rep4 sob o mesmo timestamp — cada combinação
    sobrescrevendo a repetição da anterior. Confirmado ao vivo: um upload
    individual por repetição (load/upload_one_file.py, if_generation_match=0)
    começou a rejeitar a 2ª combinação com 412 Precondition Failed porque o
    objeto de rep0 já existia no bucket, vindo da 1ª — antes disso o mesmo bug
    era silencioso localmente (sem erro, só perda de dado). A triagem nunca
    tem esse problema — só roda 1 combinação fixa (TRIAGEM_RATE/TRIAGEM_TIER)
    — então mantém o layout antigo, sem quebrar resultados já coletados."""
    if phase == "confirmacao":
        return results_dir / cell_id / phase / timestamp / f"{rate}-{tier}" / f"rep{repetition}"
    return results_dir / cell_id / phase / timestamp / f"rep{repetition}"


def target_url_for(
    cell_id: str, target_url: str | None, targets: dict[str, str] | None
) -> str:
    if targets is not None:
        return targets[cell_id]
    if target_url is not None:
        return target_url
    raise ValueError("informe --target-url ou --targets")


def _git_commit() -> str | None:
    """Hash do commit que gerou esta medição, ou None se indeterminável.

    Prioridade para a variável de ambiente `TCC_GIT_COMMIT`, injetada no build
    da imagem (docker/Dockerfile.tools). Isso não é preferência de estilo: no
    contêiner `tools` o código chega por `COPY`, então **não existe `.git`** e
    `git rev-parse HEAD` nunca teve como funcionar ali. Como a chamada usava
    `check=False` e devolvia `stdout.strip()`, o erro virava string vazia e o
    manifesto saía com `"git_commit": ""` — silenciosamente sem a informação
    que existe justamente para saber qual versão produziu cada resultado.
    Confirmado nos manifestos de results/e4-postgres/triagem/20260906T155013Z.

    Devolve None, não "": vazio se confunde com "campo não preenchido", e o
    aviso abaixo garante que a perda apareça no log da bateria, em vez de só
    no arquivo, meses depois."""
    from_env = os.environ.get("TCC_GIT_COMMIT", "").strip()
    if from_env:
        return from_env

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    commit = result.stdout.strip()
    if commit:
        return commit

    print(
        "AVISO: não foi possível determinar o commit desta medição — nem TCC_GIT_COMMIT "
        "(injetado no build da imagem) nem `git rev-parse HEAD` responderam. O manifesto "
        "vai sem rastreabilidade de versão."
    )
    return None


def build_k6_cmd(
    json_out: Path,
    cell_id: str,
    target_url: str,
    rate: int,
    k: int,
    selectivity_tier: str,
    smoke: bool,
    *,
    user_count: int | None = None,
) -> list[str]:
    # requests.ndjson (não k6-raw.json): analysis/collect.py lê daqui — uma
    # linha por requisição escrita por console.log() em load/scenarios.js,
    # não mais um join de métricas do k6 por tag (ver o comentário no topo
    # de load/scenarios.js sobre por que tag por requisição afundava o k6
    # sob cardinalidade em bateria real). --out json=... continua sendo
    # gravado (k6-raw.json) só como saída nativa de diagnóstico do k6 — o
    # chamador (run_k6 abaixo / infra/scripts/run_measurement_battery.py:
    # make_probe_fn) sobe esse arquivo pro bucket e apaga a cópia local
    # logo em seguida, nunca deixando mais de 1 k6-raw.json por vez no
    # disco da loadgen — sem isso, ele se acumula (confirmado ao vivo
    # derrubando e3-postgres com "no space left on device" na confirmação:
    # ~5GB por repetição em patamares de carga alta, sem limpeza entre
    # sondagens da busca de saturação, enche o disco de 100GB da loadgen
    # antes do fim da varredura).
    # Path(...).as_posix(): infra/scripts/run_measurement_battery.py roda
    # NO HOST (README.md, Fase 5 — "roda no HOST, não dentro do container
    # tools"), que aqui é Windows. Path(str) sem .as_posix() vira
    # WindowsPath, e str(WindowsPath) usa barra invertida — um argumento
    # `--console-output=\app\results\...` enviado por SSH pro loadgen
    # (sempre Linux) não abre nada lá. Confirmado ao vivo: k6 rodou normal
    # (a barra invertida virou parte de um nome de arquivo, não um path),
    # mas analysis/probe_report.py explodiu com FileNotFoundError procurando
    # o caminho de barra normal que ele mesmo espera. .as_posix() força
    # barra normal em qualquer host — mesmo motivo de SCENARIOS_SCRIPT
    # acima.
    requests_out = Path(json_out).with_name("requests.ndjson").as_posix()
    cmd = [
        "k6",
        "run",
        SCENARIOS_SCRIPT.as_posix(),
        "--out",
        f"json={json_out}",
        f"--console-output={requests_out}",
        # sem isso, --console-output grava cada console.log() envolto em
        # `time="..." level=info msg="<json escapado>"` (formato logrus
        # padrão do k6) em vez do JSON puro que analysis/collect.py espera
        # uma linha por linha — confirmado ao vivo testando localmente.
        "--log-format=raw",
        "-e",
        f"CELL={cell_id}",
        "-e",
        f"TARGET_URL={target_url}",
        "-e",
        f"RATE={rate}",
        "-e",
        f"K={k}",
        "-e",
        f"SELECTIVITY_TIER={selectivity_tier}",
    ]
    # USER_COUNT alimenta a CDF de Zipf de load/zipf.js. O default de lá
    # (10.000) só vale para a massa de desenvolvimento — numa medição real
    # omiti-lo faz o k6 amostrar 10.000 dos 200.948 usuários carregados, um
    # working set ~5% do pretendido que cabe em cache em qualquer tecnologia
    # (foi exatamente o que aconteceu na primeira triagem; main() abaixo
    # torna o parâmetro obrigatório fora do smoke por isso).
    if user_count is not None:
        cmd += ["-e", f"USER_COUNT={user_count}"]
    if smoke:
        # --vus/--duration na CLI do k6 são ignorados quando
        # options.scenarios já está definido no .js (é sempre o caso aqui) —
        # SMOKE_MODE=true troca o cenário inteiro dentro de scenarios.js
        # por um curto e sem threshold de SLO (ver load/scenarios.js).
        cmd += ["-e", "SMOKE_MODE=true"]
    return cmd


def build_probe_k6_cmd(
    json_out: Path,
    cell_id: str,
    target_url: str,
    rate: int,
    selectivity_tier: str,
    warmup: str,
    measure: str,
    *,
    user_count: int,
) -> list[str]:
    """Sondagem de um único patamar (load/saturation.py) — PROBE_MODE em
    load/scenarios.js, sem RATE/K fixos de constant-arrival-rate normal
    (a duração/aquecimento vêm do algoritmo de busca, não de --repetitions/
    --phase). `user_count` é obrigatório (sem default): sondagem é sempre
    medição — o S que sai dela entra em n(D) = ⌈D/S⌉, e uma sondagem sobre
    a população default de load/zipf.js mediria outro working set."""
    requests_out = Path(json_out).with_name("requests.ndjson").as_posix()
    return [
        "k6",
        "run",
        SCENARIOS_SCRIPT.as_posix(),
        "--out",
        f"json={json_out}",
        f"--console-output={requests_out}",
        "--log-format=raw",
        "-e",
        f"CELL={cell_id}",
        "-e",
        f"TARGET_URL={target_url}",
        "-e",
        f"SELECTIVITY_TIER={selectivity_tier}",
        "-e",
        f"USER_COUNT={user_count}",
        "-e",
        "PROBE_MODE=true",
        "-e",
        f"PROBE_RATE={rate}",
        "-e",
        f"PROBE_WARMUP={warmup}",
        "-e",
        f"PROBE_MEASURE={measure}",
    ]


def build_ramp_k6_cmd(
    requests_out: Path,
    cell_id: str,
    target_url: str,
    selectivity_tier: str,
    stages_json: str,
    max_vus: int,
    *,
    user_count: int,
) -> list[str]:
    """Rampa de estresse com foco no banco (STRESS_RAMP_MODE em
    load/scenarios.js, docs/DESIGN.md "Experimento complementar").

    Três diferenças deliberadas em relação a `build_probe_k6_cmd`:

    1. **Sem `--out json=`.** `k6-raw.json` é só diagnóstico nativo, pesa
       ~524 B/requisição (4x a NDJSON) e a proteção que o mantém sob
       controle — upload e `unlink` a cada repetição — não existe numa
       rampa contínua, que é UMA execução do k6. Com ele, e3-postgres
       (~169 M requisições) e e3-valkey (~208 M) estourariam o disco de
       100 GB do gerador.
    2. **Cronograma inteiro por env.** `stages_json` vem de
       `load/ramp.py:build_step_schedule`, já validado pelo pré-voo de
       disco. Recalcular a geometria dentro do JS duplicaria a regra.
    3. **`max_vus` explícito.** A heurística `RATE * 2` de scenarios.js
       pediria 100.000 VUs a 50k req/s, e o custo de memória por VU faria o
       gerador derrubar o offered_ratio — envenenando em silêncio a própria
       medição.
    """
    return [
        "k6",
        "run",
        SCENARIOS_SCRIPT.as_posix(),
        f"--console-output={Path(requests_out).as_posix()}",
        "--log-format=raw",
        "-e",
        f"CELL={cell_id}",
        "-e",
        f"TARGET_URL={target_url}",
        "-e",
        f"SELECTIVITY_TIER={selectivity_tier}",
        "-e",
        f"USER_COUNT={user_count}",
        "-e",
        "STRESS_RAMP_MODE=true",
        "-e",
        f"RAMP_STAGES={stages_json}",
        "-e",
        f"RAMP_MAX_VUS={max_vus}",
    ]


def run_k6(
    cell_id: str,
    repetition: int,
    target_url: str,
    phase: str,
    rate: int,
    k: int,
    selectivity_tier: str,
    smoke: bool,
    out_dir: Path,
    region: str | None = None,
    zone: str | None = None,
    user_count: int | None = None,
    results_bucket: str | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_out = out_dir / "k6-raw.json"
    cmd = build_k6_cmd(
        json_out, cell_id, target_url, rate, k, selectivity_tier, smoke, user_count=user_count
    )

    manifest = {
        "cell_id": cell_id,
        "repetition": repetition,
        "phase": phase,
        "target_url": target_url,
        "rate": rate,
        "k": k,
        "selectivity_tier": selectivity_tier,
        "smoke": smoke,
        # População amostrada pelo Zipf do k6 — None só em smoke (usa o
        # default dev-scale de load/zipf.js). Registrado para que um
        # resultado arquivado diga sobre QUAL base de usuários foi medido,
        # e para analysis/collect.py calcular a razão de vazão ofertada.
        "user_count": user_count,
        # Região/zona no manifesto, ao lado do hash do commit e pelo mesmo
        # motivo: sem elas não há como saber, meses depois, em que região uma
        # medição arquivada foi feita — e a região muda o preço de instância e
        # de disco, ou seja, muda o modelo de custo inteiro
        # (analysis/report.py). Ficam None quando a bateria roda fora da
        # orquestração de nuvem (ex.: ambiente local).
        "region": region,
        "zone": zone,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    result = subprocess.run(cmd, check=False)
    if results_bucket and json_out.exists():
        # Sobe k6-raw.json e apaga a cópia local imediatamente — nunca deixa
        # mais de 1 por vez no disco da loadgen (ver comentário em
        # build_k6_cmd acima sobre o "no space left on device" que isso já
        # causou). Roda ANTES do tratamento de returncode abaixo: mesmo um
        # k6 exit 99 (SLO violado) já escreveu um k6-raw.json válido que
        # precisa subir e ser limpo do mesmo jeito.
        subprocess.run(
            [sys.executable, "load/upload_one_file.py", str(json_out), results_bucket, f"{cell_id}/{out_dir.relative_to(RESULTS_DIR / cell_id).as_posix()}/k6-raw.json"],
            check=True,
        )
        json_out.unlink()
    if result.returncode == 99:
        # k6 usa o exit code 99 especificamente para "o teste rodou até o
        # fim, mas um ou mais thresholds (load/scenarios.js: p99<200ms,
        # error rate<1%) não passaram" — documentado pelo próprio k6, não é
        # um crash. É exatamente o resultado esperado de uma célula
        # dominada/mais lenta sob a carga fixa da triagem: um resultado de
        # medição válido, não um erro. Tratar como fatal (como antes,
        # check=True) abortava a bateria inteira e descartava o
        # k6-raw.json já escrito — confirmado ao vivo rodando e1-postgres,
        # que não aguenta 1000 req/s (89.88% de falha, p95=16s).
        print(
            f"AVISO: thresholds de SLO não atingidos para {cell_id} rep{repetition} "
            "(k6 exit 99) — resultado válido (célula não atende ao SLO sob esta carga), "
            "não interrompe a bateria."
        )
    elif result.returncode != 0:
        result.check_returncode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cells", nargs="*", default=None)
    parser.add_argument("--target-url")
    parser.add_argument("--targets", type=Path)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument(
        "--repetition-index",
        type=int,
        default=None,
        help="roda só esta repetição específica (ignora --repetitions) em vez de 0..N-1 — "
        "usado por infra/scripts/run_measurement_battery.py para invocar uma repetição por "
        "vez, cada uma na sua própria sessão SSH (uma queda de conexão no meio de uma "
        "combinação inteira, encadeando N repetições numa sessão só, perdia todas as já "
        "rodadas nela — confirmado ao vivo 2x contra e3-valkey).",
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--phase", default="triagem", choices=["triagem", "confirmacao", "smoke"]
    )
    parser.add_argument("--rate", type=int, default=100)
    parser.add_argument("--k", type=int, default=20)
    parser.add_argument(
        "--selectivity-tier", default="medium", choices=["high", "medium", "low"]
    )
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument(
        "--timestamp",
        default=None,
        help="reusa um timestamp já existente (%%Y%%m%%dT%%H%%M%%SZ) em vez de gerar um novo — "
        "usado por infra/scripts/run_measurement_battery.py para a varredura e a rampa curta de "
        "saturação (load/saturation.py) caírem no mesmo diretório results/<cell>/<phase>/<ts>/.",
    )
    parser.add_argument(
        "--region",
        default=None,
        help="região GCP da medição — gravada no manifest.json para a execução ser "
        "reprodutível (a região muda o preço de instância/disco e, com isso, o modelo de "
        "custo de analysis/report.py). Passada por "
        "infra/scripts/run_measurement_battery.py.",
    )
    parser.add_argument("--zone", default=None, help="zona GCP da medição — idem --region.")
    parser.add_argument(
        "--results-bucket",
        default=None,
        help="bucket de resultados — quando dado, cada k6-raw.json individual sobe pra "
        "gs://<bucket>/<cell>/<phase>/<timestamp>/rep<N>/k6-raw.json e a cópia local é apagada "
        "logo em seguida (load/upload_one_file.py), em vez de acumular no disco da loadgen até "
        "o upload final em bloco de load/upload_results.py. Sem isto (ex.: --smoke local), o "
        "k6-raw.json de cada repetição fica no disco local, como antes.",
    )
    parser.add_argument(
        "--user-count",
        type=int,
        default=None,
        help="tamanho da base de usuários carregada, injetado como USER_COUNT no k6 "
        "(load/zipf.js). Obrigatório fora de --smoke: o default do zipf.js (10.000) só vale "
        "para a massa de desenvolvimento — numa medição real, omitir isto faz o Zipf amostrar "
        "uma fração da base carregada, encolhendo o working set (docs/DESIGN.md, 'Protocolo "
        "de medição').",
    )
    args = parser.parse_args(argv)

    if not args.smoke and args.user_count is None:
        parser.error(
            "--user-count é obrigatório fora de --smoke (docs/DESIGN.md: o Zipf amostra a "
            "base de usuários INTEIRA do ambiente; sem isto o k6 usaria o default dev-scale "
            "de load/zipf.js)."
        )

    cell_ids = args.cells or list_viable_cell_ids()
    order = shuffled_cell_order(cell_ids, args.seed)
    print(f"ordem embaralhada (seed={args.seed}): {order}")

    targets = json.loads(args.targets.read_text()) if args.targets else None
    timestamp = args.timestamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    run_plan = (
        [(cell_id, args.repetition_index) for cell_id in order]
        if args.repetition_index is not None
        else build_run_plan(order, args.repetitions)
    )
    for cell_id, repetition in run_plan:
        url = target_url_for(cell_id, args.target_url, targets)
        out_dir = combo_out_dir(RESULTS_DIR, cell_id, args.phase, timestamp, args.rate, args.selectivity_tier, repetition)
        run_k6(
            cell_id,
            repetition,
            url,
            args.phase,
            args.rate,
            args.k,
            args.selectivity_tier,
            args.smoke,
            out_dir,
            region=args.region,
            zone=args.zone,
            user_count=args.user_count,
            results_bucket=args.results_bucket,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
