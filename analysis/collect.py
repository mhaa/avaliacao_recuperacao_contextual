"""Consolida a saída bruta de load/run_battery.py (requests.ndjson +
manifest.json, um por repetição) no formato de resultados de
docs/ARCHITECTURE.md, "Coleta de resultados": latencies.parquet e summary.json.
resources.csv (analysis/resources.py) e storage.json (analysis/
storage_size.py) são coletados à parte — dependem de infraestrutura viva
(containers/serviço no ar), não só do arquivo bruto do k6.

Percentis são calculados aqui em Python (polars), não a partir do
`handleSummary()` do k6: assim latencies.parquet e summary.json usam
exatamente a mesma fonte e o mesmo método de quantil que
analysis/stats.py vai reamostrar depois — duas implementações de percentil
(uma em JS, outra em Python) poderiam divergir e não haveria como saber
qual está certa.

Uso:
    docker compose run --rm --entrypoint python tools analysis/collect.py \\
        results/e1-postgres/triagem/<timestamp>/rep0
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import polars as pl

# Escopo de medição: docs/DESIGN.md, "2 min de aquecimento descartados + 5 min
# de medição" (tag `measurement`) — nunca `warmup`.
MEASUREMENT_SCENARIOS = frozenset({"measurement"})

# Sondagem de vazão de saturação (load/saturation.py, load/scenarios.js
# PROBE_MODE=true) — usado explicitamente por
# infra/scripts/run_measurement_battery.py, nunca no default (mesmo padrão
# de analysis/smoke_report.py:SMOKE_SCENARIOS).
PROBE_SCENARIOS = frozenset({"probe"})

# docs/DESIGN.md, "Vazão ofertada verificada, não presumida": abaixo desta
# fração da taxa-alvo, o k6 esgotou maxVUs e descartou chegadas — as
# latências registradas são só das requisições sobreviventes, e o modelo
# aberto deixou de valer. 0,95 é folgado para o jitter de agendamento do
# constant-arrival-rate (que erra por ±1-2%) e distante dos déficits reais
# de sobrecarga (e1-valkey na triagem entregou ~43% do alvo). Único lugar
# desta constante: analysis/probe_report.py importa daqui para o veredito
# de sondagem usar o MESMO limiar que marca as repetições de carga fixa.
MIN_OFFERED_RATIO = 0.95


def parse_requests_ndjson(
    path: Path, scenarios: frozenset[str] = MEASUREMENT_SCENARIOS
) -> pl.DataFrame:
    """latencies.parquet: uma linha por requisição — timestamp, latência,
    status, returned_count. Lida direto de `path`, o arquivo que
    load/scenarios.js escreve via console.log() por requisição (uma linha
    JSON já com os 4 campos), capturado com `k6 run --console-output=path`.

    Antes disso era um join de duas MÉTRICAS do k6 (http_req_duration +
    a Trend customizada returned_count) pelo tag `request_id` — abandonado
    porque tag com valor único por requisição faz o motor de métricas do k6
    registrar uma série temporal nova a cada requisição, e uma bateria real
    (1000 req/s por minutos contínuos) afundava o próprio processo k6 sob
    essa cardinalidade (confirmado ao vivo: p99 de 10-25s medido pelo k6
    enquanto o serviço e a rede respondiam em ~1-2ms sob a mesma carga
    testada manualmente). O k6 não tem suporte estável a tag de alta
    cardinalidade não-indexada (github.com/grafana/k6/issues/2584, ainda em
    aberto) — logging estruturado em vez de tag é a recomendação oficial do
    projeto para correlação por requisição.

    `scenarios` default é MEASUREMENT_SCENARIOS (uso normal — nunca conta
    `warmup`). infra/scripts/cloud_smoke_test.py passa
    `scenarios={"smoke"}` para o mesmo parser, sobre o cenário de smoke de
    load/scenarios.js — nunca o padrão, pra não haver risco de dado de
    smoke entrar em MEASUREMENT_SCENARIOS por engano."""
    rows: list[dict] = []

    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("scenario") not in scenarios:
                continue
            rows.append(
                {
                    "timestamp": event["timestamp"],
                    "latency_ms": float(event["latency_ms"]),
                    "status": int(event["status"]),
                    "returned_count": event["returned_count"],
                }
            )

    df = pl.DataFrame(
        rows,
        schema={
            "timestamp": pl.Utf8,
            "latency_ms": pl.Float64,
            "status": pl.Int32,
            "returned_count": pl.Int32,
        },
    )
    # k6 emite timestamps ISO-8601 em UTC com precisão de nanossegundos
    # ("...395282854Z") — `time_zone="UTC"` é necessário para o polars não
    # rejeitar o "Z" por falta de fuso horário explícito. Sem `if
    # df.is_empty()`: um run vazio (ex.: smoke test, sem pontos com
    # scenario in MEASUREMENT_SCENARIOS) precisa do MESMO schema
    # (`timestamp` como Datetime, não String) que um run com dados, ou
    # concatenar latencies.parquet de repetições diferentes quebraria.
    return df.with_columns(
        pl.col("timestamp").str.to_datetime(time_unit="ns", time_zone="UTC")
    )


def build_summary(latencies_df: pl.DataFrame) -> dict:
    """p50/p95/p99/p99,9, vazão, taxa de erro (docs/DESIGN.md, "Métricas") —
    NUNCA latência média (docs/DESIGN.md, "regra de ouro": a distribuição é
    assimétrica, a média esconde a cauda). `cache_hit_rate` fica None: o
    A-1/A-2 (camadas de cache da Fase 2, hipótese H3) ainda não existem —
    todas as células atuais rodam com `cache: none`."""
    if latencies_df.is_empty():
        return {
            "request_count": 0,
            "error_rate": None,
            "throughput_rps": None,
            "latency_ms_p50": None,
            "latency_ms_p95": None,
            "latency_ms_p99": None,
            "latency_ms_p999": None,
            "cache_hit_rate": None,
        }

    n = latencies_df.height
    error_count = latencies_df.filter(pl.col("status") >= 400).height
    span_seconds = (
        latencies_df["timestamp"].max() - latencies_df["timestamp"].min()
    ).total_seconds()
    quantile = latencies_df["latency_ms"].quantile

    return {
        "request_count": n,
        "error_rate": error_count / n,
        "throughput_rps": n / span_seconds if span_seconds > 0 else None,
        "latency_ms_p50": quantile(0.50),
        "latency_ms_p95": quantile(0.95),
        "latency_ms_p99": quantile(0.99),
        "latency_ms_p999": quantile(0.999),
        "cache_hit_rate": None,
    }


def offered_load_fields(summary: dict, target_rate: float | None) -> dict:
    """`target_rate`/`offered_ratio`/`offered_load_ok` para o summary.json.

    A razão usa a vazão medida sobre o span do próprio arquivo
    (`throughput_rps`), não uma duração fixa de cenário — assim não acopla
    este módulo ao '5m' de load/scenarios.js. `offered_load_ok=False`
    significa que o k6 descartou chegadas por esgotar maxVUs (docs/DESIGN.md,
    "Vazão ofertada verificada, não presumida"): as latências desta repetição
    são só das requisições sobreviventes. Tudo None quando não há taxa-alvo
    conhecida (sem manifest, smoke) ou nenhuma requisição foi medida."""
    throughput = summary.get("throughput_rps")
    if target_rate is None or not target_rate or throughput is None:
        return {"target_rate": target_rate, "offered_ratio": None, "offered_load_ok": None}
    ratio = throughput / target_rate
    return {
        "target_rate": target_rate,
        "offered_ratio": ratio,
        "offered_load_ok": ratio >= MIN_OFFERED_RATIO,
    }


def collect(run_dir: Path, scenarios: frozenset[str] = MEASUREMENT_SCENARIOS) -> None:
    latencies_df = parse_requests_ndjson(run_dir / "requests.ndjson", scenarios=scenarios)
    latencies_df.write_parquet(run_dir / "latencies.parquet")

    summary = build_summary(latencies_df)

    # A taxa-alvo vem do manifest.json que load/run_battery.py escreve ao
    # lado do requests.ndjson. Ausência é normal (sondagens de saturação não
    # têm manifesto; probe_report.py faz o mesmo portão pelo caminho dele) —
    # nesse caso os campos ficam None, nunca inventados.
    manifest_path = run_dir / "manifest.json"
    target_rate = None
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        if not manifest.get("smoke"):
            target_rate = manifest.get("rate")
    summary.update(offered_load_fields(summary, target_rate))
    if summary["offered_load_ok"] is False:
        print(
            f"AVISO: {run_dir} — vazão ofertada de {summary['throughput_rps']:.0f} req/s ficou "
            f"abaixo de {MIN_OFFERED_RATIO:.0%} do alvo de {target_rate} req/s: o k6 descartou "
            "chegadas (maxVUs esgotado) e as latências registradas são só das requisições "
            "sobreviventes — não leia esta repetição como modelo aberto sustentado."
        )

    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"Coletado: {run_dir} ({summary['request_count']} requisições)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "run_dir", type=Path, help="results/<cell-id>/<phase>/<timestamp>/rep<N>"
    )
    args = parser.parse_args(argv)
    collect(args.run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
