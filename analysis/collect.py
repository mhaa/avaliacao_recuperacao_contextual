"""Consolida a saída bruta de load/run_battery.py (k6-raw.json + manifest.json,
um por repetição) no formato de resultados de IMPLEMENTACAO.md, "Coleta de
resultados": latencies.parquet e summary.json. resources.csv (analysis/
resources.py) e storage.json (analysis/storage_size.py) são coletados à
parte — dependem de infraestrutura viva (containers/serviço no ar), não só
do arquivo bruto do k6.

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

# Escopo de medição: CONTEXTO.md, "2 min de aquecimento descartados + 5 min
# de medição" (tag `measurement`) — nunca `warmup`.
MEASUREMENT_SCENARIOS = frozenset({"measurement"})

# Sondagem de vazão de saturação (load/saturation.py, load/scenarios.js
# PROBE_MODE=true) — usado explicitamente por
# infra/scripts/run_measurement_battery.py, nunca no default (mesmo padrão
# de analysis/smoke_report.py:SMOKE_SCENARIOS).
PROBE_SCENARIOS = frozenset({"probe"})


def parse_k6_ndjson(path: Path, scenarios: frozenset[str] = MEASUREMENT_SCENARIOS) -> pl.DataFrame:
    """latencies.parquet: uma linha por requisição — timestamp, latência,
    status, returned_count. Junta http_req_duration (built-in) e
    returned_count (customizada, ver load/scenarios.js) pelo tag
    `request_id` que as duas carregam.

    `scenarios` default é MEASUREMENT_SCENARIOS (uso normal — nunca conta
    `warmup`). infra/scripts/cloud_smoke_test.py passa
    `scenarios={"smoke"}` para o mesmo parser, sobre o cenário de smoke de
    load/scenarios.js — nunca o padrão, pra não haver risco de dado de
    smoke entrar em MEASUREMENT_SCENARIOS por engano."""
    durations: list[dict] = []
    returned_by_request: dict[str, int] = {}

    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("type") != "Point":
                continue
            data = event["data"]
            tags = data.get("tags") or {}
            if tags.get("scenario") not in scenarios:
                continue

            if event["metric"] == "http_req_duration":
                durations.append(
                    {
                        "timestamp": data["time"],
                        "latency_ms": data["value"],
                        "status": int(tags.get("status", 0)),
                        "request_id": tags.get("request_id"),
                    }
                )
            elif event["metric"] == "returned_count":
                request_id = tags.get("request_id")
                if request_id is not None:
                    returned_by_request[request_id] = int(data["value"])

    rows = [
        {
            "timestamp": d["timestamp"],
            "latency_ms": d["latency_ms"],
            "status": d["status"],
            "returned_count": returned_by_request.get(d["request_id"]),
        }
        for d in durations
    ]
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
    """p50/p95/p99/p99,9, vazão, taxa de erro (CONTEXTO.md, "Métricas") —
    NUNCA latência média (CONTEXTO.md, "regra de ouro": a distribuição é
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


def collect(run_dir: Path, scenarios: frozenset[str] = MEASUREMENT_SCENARIOS) -> None:
    latencies_df = parse_k6_ndjson(run_dir / "k6-raw.json", scenarios=scenarios)
    latencies_df.write_parquet(run_dir / "latencies.parquet")

    summary = build_summary(latencies_df)
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
