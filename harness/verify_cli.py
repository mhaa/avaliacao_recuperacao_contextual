"""CLI fina em volta de harness/verify.py — mesma lógica já usada por
tests/acceptance/test_harness_all_cells.py, mas chamável direto contra uma
célula específica sem pytest (necessário para o smoke test em nuvem:
gate de corretude antes de gastar tempo/dinheiro com carga, ver
IMPLEMENTACAO.md/CLAUDE.md, "Correctness before latency").

Uso:
    docker compose run --rm --entrypoint python tools -m harness.verify_cli --cell e1-postgres

Host/porta/credenciais do storage seguem exatamente as mesmas regras de
core/registry.py:build_storage — localmente vêm de cells/<id>.yaml, na
nuvem STORAGE_HOST/STORAGE_PORT sobrescrevem.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from core.config import load_cell_config
from core.registry import build_storage, build_strategy
from harness.oracle import load_oracle_cases
from harness.verify import format_report, verify_cell


async def _run(cell_id: str) -> bool:
    config = load_cell_config(cell_id)
    strategy = build_strategy(config)
    storage = build_storage(config)
    cases = load_oracle_cases()
    report = await verify_cell(strategy, storage, cases)
    print(format_report(report, cell_id))
    return report.all_passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cell", required=True, help="id da célula, ex.: e1-postgres")
    args = parser.parse_args(argv)

    all_passed = asyncio.run(_run(args.cell))
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
