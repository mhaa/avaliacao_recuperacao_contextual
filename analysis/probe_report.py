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

Uso:
    python analysis/probe_report.py /app/results/_saturation/e1-postgres/short-0-1000/requests.ndjson
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from analysis.collect import build_summary, parse_requests_ndjson

PROBE_SCENARIOS = frozenset({"probe"})

# docs/DESIGN.md, "Protocolo de medição": "SLO: p99 > 200 ms ou taxa de erro > 1%."
SLO_P99_MS = 200.0
SLO_ERROR_RATE = 0.01


def violated_slo(summary: dict) -> bool:
    p99 = summary["latency_ms_p99"]
    error_rate = summary["error_rate"]
    if p99 is None or error_rate is None:
        # Nenhuma requisição com scenario=probe foi parseada — não é "passou
        # o SLO", é um resultado inválido; tratar como violação evita que a
        # busca de saturação avance com um patamar que não mediu nada.
        return True
    return p99 > SLO_P99_MS or error_rate > SLO_ERROR_RATE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ndjson_path", type=Path)
    args = parser.parse_args(argv)

    df = parse_requests_ndjson(args.ndjson_path, scenarios=PROBE_SCENARIOS)
    summary = build_summary(df)
    violated = violated_slo(summary)

    print(
        f"PROBE_RESULT violated_slo={violated} p99={summary['latency_ms_p99']} "
        f"error_rate={summary['error_rate']} request_count={summary['request_count']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
