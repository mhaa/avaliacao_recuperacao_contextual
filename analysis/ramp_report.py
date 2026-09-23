"""Relatório por degrau da rampa de estresse (docs/DESIGN.md, "Experimento
complementar — estresse com foco no banco").

Roda NA VM DO GERADOR, dentro do container de ferramentas, como
analysis/probe_report.py — e pelo mesmo motivo de topologia: é o lado que já
sobe seus resultados direto para o bucket (load/upload_results.py), então o
artefato nasce onde o destino primário o alcança, sem passar pelo host.

Nenhuma regra é reimplementada aqui. Percentis e vazão vêm de
`build_summary`, a razão de oferta de `offered_load_fields`, e o veredito de
SLO de `probe_report.violated_slo` — os mesmos que decidem a bateria
principal. Duas implementações do mesmo julgamento divergem com o tempo; esta
é a razão de o módulo ser curto.

Diferença de POSTURA em relação ao resto do projeto: passado o joelho,
`offered_ratio` cai abaixo de 0,95 e `violated_slo` passa a ser True por dois
motivos distintos colapsados num bit (SLO estourado vs. carga nunca ofertada).
Em toda a bateria principal esse regime marca dado inválido. Aqui ele é o
objeto de estudo — então `offered_ratio` e `offered_load_ok` viajam como
campos de primeira classe POR DEGRAU, para que quem lê saiba exatamente quais
percentis são só dos sobreviventes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import polars as pl

from analysis.collect import (
    RAMP_EXTRA_FIELDS,
    RAMP_SCENARIOS,
    build_summary,
    offered_load_fields,
    parse_requests_ndjson,
)
from analysis.probe_report import violated_slo
from load.ramp import RampStepResult, classify_recovery

# Ordem canônica das fases. A rampa é uma trajetória, não um conjunto: o mesmo
# `rate` aparece na subida e na descida, e é justamente o par que revela a
# histerese — ordenar por taxa perderia essa informação.
_PHASE_ORDER = {"coarse_up": 0, "fine_up": 1, "plateau": 2, "fine_down": 3}


def _isoformat(value) -> str | None:
    return value.isoformat() if value is not None else None


def step_results(
    latencies_df: pl.DataFrame, ignore_latency: bool = False
) -> list[RampStepResult]:
    """Um `RampStepResult` por (degrau, fase), na ordem em que a rampa os
    percorreu.

    O agrupamento é por `step_rate` E `step_phase` porque a descida repete as
    taxas da subida: agrupar só por taxa fundiria os dois lados da histerese
    num número só, apagando exatamente o que se quer medir.

    `ignore_latency`: mesmo parâmetro de `probe_report.violated_slo` — só a
    campanha de estresse (`infra/scripts/run_stress_ramp.py`) passa True,
    nunca a bateria principal. Ver `violated_slo()` para o motivo.
    """
    if latencies_df.is_empty():
        return []

    results: list[RampStepResult] = []
    groups = latencies_df.group_by(["step_rate", "step_phase"], maintain_order=True)
    for (rate, phase), group in groups:
        summary = build_summary(group.drop(["step_rate", "step_phase"]))
        offered = offered_load_fields(summary, float(rate))
        results.append(
            RampStepResult(
                rate=int(rate),
                phase=phase,
                throughput_rps=summary["throughput_rps"],
                latency_ms_p50=summary["latency_ms_p50"],
                latency_ms_p95=summary["latency_ms_p95"],
                latency_ms_p99=summary["latency_ms_p99"],
                latency_ms_p999=summary["latency_ms_p999"],
                error_rate=summary["error_rate"],
                request_count=summary["request_count"],
                offered_ratio=offered["offered_ratio"],
                offered_load_ok=offered["offered_load_ok"],
                violated_slo=violated_slo(
                    summary, offered["offered_ratio"], ignore_latency=ignore_latency
                ),
                started_at=_isoformat(group["timestamp"].min()),
                ended_at=_isoformat(group["timestamp"].max()),
            )
        )

    results.sort(key=lambda r: _PHASE_ORDER.get(r.phase, 99))
    return results


def _median_spacing(rates: list[int]) -> int | None:
    """Espaçamento típico entre degraus consecutivos de uma fase — lido do
    DADO, não de um parâmetro, para o relatório continuar correto mesmo se
    o cronograma tiver sido montado à mão."""
    ordered = sorted(rates)
    gaps = [b - a for a, b in zip(ordered, ordered[1:]) if b > a]
    if not gaps:
        return None
    return sorted(gaps)[len(gaps) // 2]


def knee_summary(results: list[RampStepResult]) -> dict:
    """Onde o joelho caiu, e com que resolução foi medido.

    Existe porque o cronograma é estático: a fase fina cobre `[0,6·K, 1,3·K]`
    em torno do joelho PROJETADO, e uma projeção errada em mais de ~40% para
    baixo joga a violação na fase grossa — medida em degraus de 1.000 req/s
    e 30 s, não no passo fino. O número sairia assim mesmo, sem nenhum erro
    visível, com uma incerteza 10x maior do que a pretendida.

    Pior: com o joelho abaixo da janela, TODA a fase fina roda em sobrecarga,
    nenhum degrau de subida fica saudável, e `classify_recovery` devolve
    "undetermined" — a campanha inteira é paga sem produzir o veredito.

    Daí este resumo viajar no artefato: quem lê precisa saber se o joelho foi
    resolvido com a precisão que a campanha prometeu.
    """
    ascent = [r for r in results if r.phase in ("coarse_up", "fine_up")]
    fine_rates = [r.rate for r in results if r.phase == "fine_up"]
    violations = [r for r in ascent if r.violated_slo]

    if not violations:
        return {
            "first_violation_rate": None,
            "first_violation_phase": None,
            "resolution_rps": None,
            "resolved_in_fine_phase": None,
            "warning": (
                "A rampa terminou sem violar o SLO: o joelho está ACIMA do topo "
                "oferecido. Refaça com --knee maior."
            ),
        }

    first = min(violations, key=lambda r: r.rate)
    phase_rates = [r.rate for r in ascent if r.phase == first.phase]
    resolution = _median_spacing(phase_rates)
    in_fine = first.phase == "fine_up"

    warning = None
    if not in_fine:
        low = min(fine_rates) if fine_rates else None
        warning = (
            f"Joelho em {first.rate} req/s caiu na fase GROSSA (resolução "
            f"~{resolution} req/s), abaixo do início da fase fina ({low} req/s). "
            f"A precisão prometida pela campanha NÃO foi alcançada e a fase fina "
            f"rodou inteira em sobrecarga. Refaça com --knee ~{first.rate}."
        )

    return {
        "first_violation_rate": first.rate,
        "first_violation_phase": first.phase,
        "resolution_rps": resolution,
        "resolved_in_fine_phase": in_fine,
        "warning": warning,
    }


def build_ramp_report(
    requests_path: Path,
    cell_id: str,
    tier: str,
    generator_cpu_percent: float | None = None,
    ignore_latency: bool = False,
) -> dict:
    latencies_df = parse_requests_ndjson(
        requests_path, scenarios=RAMP_SCENARIOS, extra_fields=RAMP_EXTRA_FIELDS
    )
    results = step_results(latencies_df, ignore_latency=ignore_latency)
    verdict = classify_recovery(results)

    return {
        "cell_id": cell_id,
        "tier": tier,
        # Repetir por degrau seria redundante: a CPU do gerador é lida uma
        # vez, cercando a rampa inteira. Acima de 60% a curva medida é do
        # gerador, não do banco (docs/DESIGN.md) — e aí o arquivo inteiro é
        # inválido, não um degrau dele.
        "generator_cpu_percent": generator_cpu_percent,
        "steps": [
            {
                "rate": r.rate,
                "phase": r.phase,
                "throughput_rps": r.throughput_rps,
                "latency_ms_p50": r.latency_ms_p50,
                "latency_ms_p95": r.latency_ms_p95,
                "latency_ms_p99": r.latency_ms_p99,
                "latency_ms_p999": r.latency_ms_p999,
                "error_rate": r.error_rate,
                "request_count": r.request_count,
                "offered_ratio": r.offered_ratio,
                "offered_load_ok": r.offered_load_ok,
                "violated_slo": r.violated_slo,
                "started_at": r.started_at,
                "ended_at": r.ended_at,
            }
            for r in results
        ],
        # Onde o joelho caiu e com que resolução — o cronograma é estático,
        # então uma projeção ruim degrada a precisão sem nenhum erro visível.
        "knee": knee_summary(results),
        "recovery": {
            "verdict": verdict.verdict,
            "reason": verdict.reason,
            "comparisons": [
                {
                    "rate": c.rate,
                    "metric": c.metric,
                    "ascent_value": c.ascent_value,
                    "descent_value": c.descent_value,
                    "within_tolerance": c.within_tolerance,
                }
                for c in verdict.comparisons
            ],
        },
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requests_ndjson", type=Path)
    parser.add_argument("--cell", required=True)
    parser.add_argument("--tier", required=True)
    parser.add_argument("--generator-cpu-percent", type=float, default=None)
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="ramp_<tier>.json. Nome deliberadamente FORA do glob saturation*.json "
        "de analysis/report.py: esta rampa mede além da saturação de propósito, "
        "e o número nunca pode alimentar n(D) = ⌈D/S⌉.",
    )
    parser.add_argument(
        "--ignore-latency-slo",
        action="store_true",
        help="ignora p99>200ms no veredito por degrau — só taxa de erro >1%% conta (além do "
        "portão de vazão ofertada, nunca ignorado). Espelha --ignore-latency-slo de "
        "analysis/probe_report.py; usado só pela campanha de estresse "
        "(infra/scripts/run_stress_ramp.py). Nunca usado fora dela.",
    )
    args = parser.parse_args(argv)

    report = build_ramp_report(
        args.requests_ndjson,
        args.cell,
        args.tier,
        args.generator_cpu_percent,
        ignore_latency=args.ignore_latency_slo,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))

    degraded = sum(1 for s in report["steps"] if s["offered_load_ok"] is False)
    knee = report["knee"]
    print(
        f"RAMP_REPORT cell={args.cell} tier={args.tier} "
        f"steps={len(report['steps'])} degraus_com_oferta_deficitaria={degraded} "
        f"joelho={knee['first_violation_rate']} resolucao={knee['resolution_rps']} "
        f"recovery={report['recovery']['verdict']} out={args.out}"
    )
    # Alto e visível: uma campanha que custou horas de VM e não resolveu o
    # joelho na precisão prometida precisa dizer isso na cara de quem roda,
    # não só num campo do JSON.
    if knee["warning"]:
        print(f"AVISO: {knee['warning']}")


if __name__ == "__main__":
    main()
