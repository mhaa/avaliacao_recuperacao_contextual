"""CLI fina que imprime um resumo de sanidade (p50/p95/p99, taxa de erro —
NUNCA média, CLAUDE.md: "Never report mean latency") de um run de smoke
test em nuvem, reusando parse_requests_ndjson()/build_summary() de
analysis/collect.py.

Existe como arquivo, não um `python -c "..."` embutido em
infra/scripts/cloud_smoke_test.py: um one-liner com aspas internas
quebrava a sintaxe do `bash -c "..."` que o envolve quando executado
remotamente via `gcloud compute ssh --command=...`.

Uso:
    python analysis/smoke_report.py /tmp/smoke-requests.ndjson
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from analysis.collect import build_summary, parse_requests_ndjson

SMOKE_SCENARIOS = frozenset({"smoke"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ndjson_path", type=Path)
    args = parser.parse_args(argv)

    df = parse_requests_ndjson(args.ndjson_path, scenarios=SMOKE_SCENARIOS)
    summary = build_summary(df)

    print(f"smoke: {summary['request_count']} requisições")
    if summary["request_count"] == 0:
        print("  nenhuma requisição com scenario=smoke foi parseada — ver k6-raw.json")
        return 1

    print(f"  taxa de erro: {summary['error_rate']:.2%}")
    print(
        f"  p50={summary['latency_ms_p50']:.1f}ms "
        f"p95={summary['latency_ms_p95']:.1f}ms "
        f"p99={summary['latency_ms_p99']:.1f}ms"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
