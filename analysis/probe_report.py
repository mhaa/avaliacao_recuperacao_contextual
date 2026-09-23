"""Sondagem de vazão de saturação (load/saturation.py) — imprime
violated_slo=True/False de uma sondagem única, para
infra/scripts/run_measurement_battery.py capturar via stdout de
`gcloud compute ssh`, sem precisar de polars no host (o host só tem
gcloud CLI + stdlib — este script roda dentro do container `tools`, onde
polars já está instalado).

Existe como arquivo, não um `python -c "..."` embutido no comando remoto —
mesmo motivo de analysis/smoke_report.py: um `-c` com aspas internas já
quebrou a sintaxe do `bash -c "..."` que o envolve quando entregue via
`gcloud compute ssh --command=...`.

Uso (1 ndjson na rampa curta da triagem, N na de confirmação — um por
repetição, todos consolidados num veredito só):
    python analysis/probe_report.py /app/results/_saturation/e1-postgres/short-0-1000/requests.ndjson
    python analysis/probe_report.py .../confirm-low-0-1000/rep0/requests.ndjson .../rep1/requests.ndjson ...
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
from pathlib import Path

import polars as pl

from analysis.collect import MIN_OFFERED_RATIO, build_summary, parse_requests_ndjson

# --decision-statistic (ver main()): duplicado como tupla de literais em vez
# de um Enum importável, porque quem também precisa desses dois valores
# (infra/scripts/run_measurement_battery.py) roda no HOST, sem polars —
# não pode importar este módulo. Manter os dois valores em sincronia é
# responsabilidade de quem editar um dos dois arquivos.
DECISION_STATISTICS = ("pooled", "median-per-repetition")

PROBE_SCENARIOS = frozenset({"probe"})

# docs/DESIGN.md, "Protocolo de medição": "SLO: p99 > 200 ms ou taxa de erro > 1%."
SLO_P99_MS = 200.0
SLO_ERROR_RATE = 0.01


def _parse_proc_stat_cpu_fields(line: str) -> tuple[int, ...]:
    """Primeira linha de /proc/stat ('cpu  user nice system idle iowait irq
    softirq steal ...'), em jiffies acumulados desde o boot."""
    fields = line.split()
    if fields[0] != "cpu":
        raise RuntimeError(f"linha de /proc/stat inesperada: {line!r}")
    return tuple(int(x) for x in fields[1:9])


def _cpu_percent_from_stat(before: tuple[int, ...], after: tuple[int, ...]) -> float:
    """Mesma aritmética de `top`/`mpstat` a partir de duas leituras de
    /proc/stat: idle_time = idle+iowait, percentual = 1 - (delta idle /
    delta total). Lido de dentro do container (`docker run --network
    host`), mas Docker não virtualiza a contagem de jiffies por container
    sem ferramentas extras (lxcfs) — o valor visto é o da VM inteira,
    exatamente o escopo que docs/DESIGN.md pede ("CPU do gerador"). Preferido
    a consultar o Cloud Monitoring (GCPMonitoringCollector,
    analysis/resources.py) para este portão: elimina a dependência de um
    pipeline de ingestão assíncrono para um dado que já está disponível
    localmente, na própria VM, no mesmo processo que decide o veredito da
    sondagem — confirmado ao vivo que a consulta ao Cloud Monitoring
    frequentemente não tem nenhum ponto amostrado na janela curta de uma
    sondagem (results/e3-postgres/triagem/20260904T022008Z/saturation.json:
    5 de 5 sondagens sem leitura)."""
    idle_before, idle_after = before[3] + before[4], after[3] + after[4]
    total_before, total_after = sum(before), sum(after)
    delta_total = total_after - total_before
    if delta_total <= 0:
        return 0.0
    delta_idle = idle_after - idle_before
    return 100.0 * (delta_total - delta_idle) / delta_total


def violated_slo(
    summary: dict, offered_ratio: float | None = None, ignore_latency: bool = False
) -> bool:
    p99 = summary["latency_ms_p99"]
    error_rate = summary["error_rate"]
    if p99 is None or error_rate is None:
        # Nenhuma requisição com scenario=probe foi parseada — não é "passou
        # o SLO", é um resultado inválido; tratar como violação evita que a
        # busca de saturação avance com um patamar que não mediu nada.
        return True
    if offered_ratio is not None and offered_ratio < MIN_OFFERED_RATIO:
        # docs/DESIGN.md, "Vazão ofertada verificada, não presumida": o k6
        # esgotou maxVUs e descartou chegadas — o patamar não foi de fato
        # oferecido, e p99/error_rate cobrem só as requisições sobreviventes.
        # Um patamar que a célula não sustenta nem receber conta como
        # violação (direção segura: subestima S em vez de superestimá-lo, e
        # S entra direto no custo via n(D) = ⌈D/S⌉).
        return True
    if ignore_latency:
        # Só a campanha de estresse (infra/scripts/run_stress_ramp.py) passa
        # isto — lá o objetivo é achar o teto real do BANCO, e o p99>200ms da
        # SLO de cliente é um limiar de experiência de usuário, não de
        # capacidade: pode disparar por fila em outra camada (serviço) antes
        # do banco saturar de fato, subestimando o joelho que a campanha quer
        # medir. A bateria principal (triagem/confirmação) nunca passa isto —
        # lá o p99 real da SLO é exatamente o que se quer medir.
        return error_rate > SLO_ERROR_RATE
    return p99 > SLO_P99_MS or error_rate > SLO_ERROR_RATE


def _per_repetition_summaries(paths: list[Path]) -> list[dict]:
    """build_summary() por ARQUIVO (uma repetição), não sobre o pool
    concatenado de todos — a inversão de uma linha que --decision-statistic
    median-per-repetition existe para fazer. Ver median_decision_summary."""
    return [build_summary(parse_requests_ndjson(path, scenarios=PROBE_SCENARIOS)) for path in paths]


def _worst_case_if_empty(summary: dict) -> tuple[float, float]:
    """(p99, error_rate) de UMA repetição, com pior caso quando ela não
    parseou nenhuma requisição — mesma regra de violated_slo ("sem dado é
    violação"), aplicada aqui pra essa repetição nunca ser silenciosamente
    excluída do cálculo da mediana entre repetições."""
    p99 = summary["latency_ms_p99"]
    error_rate = summary["error_rate"]
    if p99 is None or error_rate is None:
        return float("inf"), 1.0
    return p99, error_rate


def median_decision_summary(per_rep_summaries: list[dict]) -> dict:
    """Mediana do p99 e da taxa de erro ENTRE repetições — não o p99 do
    pool concatenado (analysis/collect.py:build_summary sobre todas juntas).
    Com N ímpar (CONFIRMATION_REPETITIONS=5), "mediana do p99 > 200ms"
    equivale a "maioria das repetições violam individualmente": responde
    "essa vazão tipicamente quebra o SLO", não "o agregado poolizado
    quebra" — o efeito diagnosticado ao vivo (e3-postgres/alta seletividade,
    4365 req/s: p99 individuais [222,419,246,232,334]ms, todas violam
    200ms, mas o pool concatenado não). Devolve um dict no formato que
    violated_slo() espera."""
    p99s, error_rates = zip(*(_worst_case_if_empty(s) for s in per_rep_summaries))
    return {
        "latency_ms_p99": statistics.median(p99s),
        "error_rate": statistics.median(error_rates),
    }


def min_offered_ratio(
    per_rep_summaries: list[dict], expected_requests_per_rep: float | None
) -> float | None:
    """Pior repetição, não a média/mediana entre elas — vazão ofertada é um
    PORTÃO DE VALIDADE (docs/DESIGN.md, "Vazão ofertada verificada, não
    presumida"), não uma métrica de desempenho onde mediana faria sentido.
    Mediana deixaria 2 de 5 repetições com déficit de oferta se esconderem
    atrás de 3 saudáveis — o mesmo problema de mascaramento que
    --decision-statistic median-per-repetition existe para corrigir na
    métrica de SLO, só que relocado pra cá. None quando o chamador não
    passou --expected-requests (compatível com invocações antigas, mesma
    regra do caminho "pooled")."""
    if expected_requests_per_rep is None:
        return None
    return min(s["request_count"] / expected_requests_per_rep for s in per_rep_summaries)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # nargs="+": a rampa de confirmação roda k6 CONFIRMATION_REPETITIONS
    # vezes (5) e passa um requests.ndjson por repetição — "consolida" no
    # docstring do módulo (acima) significa combinar todas antes de calcular
    # o p99/error_rate de UM veredito, não só ler a primeira e ignorar o
    # resto (bug real: sem nargs, argparse rejeitava os paths extras com
    # "unrecognized arguments" — nunca acionado ainda porque nenhuma célula
    # chegou à confirmação; a rampa curta da triagem sempre passa 1 só, onde
    # o bug era invisível).
    parser.add_argument("ndjson_path", type=Path, nargs="+")
    parser.add_argument(
        "--expected-requests",
        type=int,
        default=None,
        help="quantas requisições a(s) janela(s) de medição deveriam ter emitido "
        "(taxa × duração × repetições, calculado pelo orquestrador). Com isso, um déficit "
        f"além de {1 - MIN_OFFERED_RATIO:.0%} (k6 descartando chegadas por maxVUs esgotado) "
        "vira violated_slo=True — docs/DESIGN.md, 'Vazão ofertada verificada, não presumida'. "
        "Sem o argumento (compatível com invocações antigas), o portão não é avaliado e "
        "offered_ratio sai None.",
    )
    parser.add_argument(
        "--decision-statistic",
        choices=DECISION_STATISTICS,
        default="pooled",
        help="'pooled' (default, preserva o comportamento histórico): concatena todas as "
        "repetições num só dataframe e calcula UM p99/error_rate agregado. "
        "'median-per-repetition': calcula p99/error_rate de CADA repetição separadamente e usa "
        "a MEDIANA entre elas para decidir violated_slo — robusto a uma repetição isolada que "
        "puxa (ou esconde) a violação do agregado poolizado (achado ao vivo, docs/DESIGN.md). "
        "Só troca a estatística de decisão; violated_slo() em si não muda.",
    )
    parser.add_argument(
        "--ignore-latency-slo",
        action="store_true",
        help="ignora p99>200ms na decisão de violated_slo — só taxa de erro >1% conta "
        "(além do portão de vazão ofertada, que nunca é ignorado). Usado só pela campanha "
        "de estresse (infra/scripts/run_stress_ramp.py): ver violated_slo() para o motivo. "
        "Nunca usado pela bateria principal (triagem/confirmação).",
    )
    args = parser.parse_args(argv)

    per_rep_p99_ms: list[float] | None = None
    per_rep_violated: list[bool] | None = None

    if args.decision_statistic == "median-per-repetition":
        # Parseia cada ndjson SEPARADAMENTE (um build_summary por repetição)
        # — nunca concatena antes de calcular o p99, que é exatamente o
        # ponto desta estatística de decisão.
        per_rep_summaries = _per_repetition_summaries(args.ndjson_path)
        request_count = sum(s["request_count"] for s in per_rep_summaries)
        decision_summary = median_decision_summary(per_rep_summaries)
        expected_per_rep = (
            args.expected_requests / len(args.ndjson_path) if args.expected_requests else None
        )
        offered_ratio = min_offered_ratio(per_rep_summaries, expected_per_rep)
        per_rep_p99_ms = [_worst_case_if_empty(s)[0] for s in per_rep_summaries]
        per_rep_violated = [
            violated_slo(s, ignore_latency=args.ignore_latency_slo) for s in per_rep_summaries
        ]
    else:
        # "pooled" (default): comportamento histórico, intocado — concatena
        # todos os ndjson num só dataframe e calcula UM p99/error_rate
        # agregado (não é o mesmo que agregar os p99 por repetição — p99
        # não é linear em concatenação, por isso os dois modos não
        # compartilham este parsing).
        df = pl.concat(
            [parse_requests_ndjson(path, scenarios=PROBE_SCENARIOS) for path in args.ndjson_path]
        )
        decision_summary = build_summary(df)
        request_count = decision_summary["request_count"]
        # Contagem contra o esperado, não throughput/span como em
        # analysis/collect.py:offered_load_fields: aqui o df concatena
        # repetições separadas por aquecimentos e restarts de k6, então o
        # span atravessa buracos legítimos e diluiria a razão.
        offered_ratio = (
            decision_summary["request_count"] / args.expected_requests
            if args.expected_requests
            else None
        )

    violated = violated_slo(decision_summary, offered_ratio, ignore_latency=args.ignore_latency_slo)

    before = _parse_proc_stat_cpu_fields(os.environ["GENERATOR_CPU_STAT_BEFORE"])
    after = _parse_proc_stat_cpu_fields(os.environ["GENERATOR_CPU_STAT_AFTER"])
    generator_cpu_percent = _cpu_percent_from_stat(before, after)

    line = (
        f"PROBE_RESULT violated_slo={violated} p99={decision_summary['latency_ms_p99']} "
        f"error_rate={decision_summary['error_rate']} request_count={request_count} "
        f"offered_ratio={offered_ratio} "
        f"generator_cpu_percent={generator_cpu_percent} "
        f"ignore_latency_slo={args.ignore_latency_slo}"
    )
    if per_rep_p99_ms is not None and per_rep_violated is not None:
        # Tokens aditivos, só no modo mediana — sem espaços (vírgula como
        # separador), compatível com o tokenizer `tok.split("=", 1)` de
        # infra/scripts/run_measurement_battery.py:_parse_probe_result, que
        # já ignora tokens desconhecidos.
        per_rep_p99_str = ",".join(str(v) for v in per_rep_p99_ms)
        per_rep_violated_str = ",".join(str(v) for v in per_rep_violated)
        line += f" per_rep_p99_ms={per_rep_p99_str} per_rep_violated={per_rep_violated_str}"
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
