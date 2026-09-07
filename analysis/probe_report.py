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
import sys
from pathlib import Path

import polars as pl

from analysis.collect import MIN_OFFERED_RATIO, build_summary, parse_requests_ndjson

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


def violated_slo(summary: dict, offered_ratio: float | None = None) -> bool:
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
    return p99 > SLO_P99_MS or error_rate > SLO_ERROR_RATE


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
    args = parser.parse_args(argv)

    df = pl.concat(
        [parse_requests_ndjson(path, scenarios=PROBE_SCENARIOS) for path in args.ndjson_path]
    )
    summary = build_summary(df)
    # Contagem contra o esperado, não throughput/span como em
    # analysis/collect.py:offered_load_fields: aqui o df concatena
    # repetições separadas por aquecimentos e restarts de k6, então o span
    # atravessa buracos legítimos e diluiria a razão.
    offered_ratio = (
        summary["request_count"] / args.expected_requests if args.expected_requests else None
    )
    violated = violated_slo(summary, offered_ratio)

    before = _parse_proc_stat_cpu_fields(os.environ["GENERATOR_CPU_STAT_BEFORE"])
    after = _parse_proc_stat_cpu_fields(os.environ["GENERATOR_CPU_STAT_AFTER"])
    generator_cpu_percent = _cpu_percent_from_stat(before, after)

    print(
        f"PROBE_RESULT violated_slo={violated} p99={summary['latency_ms_p99']} "
        f"error_rate={summary['error_rate']} request_count={summary['request_count']} "
        f"offered_ratio={offered_ratio} "
        f"generator_cpu_percent={generator_cpu_percent}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
