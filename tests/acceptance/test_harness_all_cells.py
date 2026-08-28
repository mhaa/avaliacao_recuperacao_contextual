"""Roda a suíte de verificação (harness/verify.py) contra todas as células
viáveis já implementadas, sobre os 1000 casos do oráculo. Ver CONTEXTO.md,
"regra de ouro da implementação", e IMPLEMENTACAO.md, "## Não fazer": nunca
medir latência de uma célula que não passou aqui.

Exige, antes de rodar:
    docker compose up -d postgres
    docker compose run --rm tools python schemas/postgres/load_oracle_fixture.py
    docker compose run --rm tools -m integration tests/acceptance/test_harness_all_cells.py -v

A lista de células viáveis agora vem de `cells/*.yaml` + `core/registry.py`
(Etapa 6) — não é mais hardcoded aqui. Não tem e4-scylla nem e3-opensearch
(inviáveis por CONTEXTO.md) — ver
tests/acceptance/test_infeasible_cells_fail_at_startup.py.
"""

from __future__ import annotations

import pytest

from core.config import load_cell_config
from core.registry import build_storage, build_strategy
from harness.oracle import load_oracle_cases
from harness.verify import format_report, verify_cell

pytestmark = pytest.mark.integration

VIABLE_CELL_IDS = [
    "e1-postgres",
    "e2-postgres",
    "e3-postgres",
    "e4-postgres",
    "e1-valkey",
    "e2-valkey",
    "e3-valkey",
    "e4-valkey",
    "e1-scylla",
    "e2-scylla",
    "e3-scylla",
    "e1-opensearch",
    "e2-opensearch",
    "e4-opensearch",
]


@pytest.mark.parametrize("cell_id", VIABLE_CELL_IDS)
async def test_cell_matches_oracle(cell_id):
    config = load_cell_config(cell_id)
    strategy = build_strategy(config)
    storage = build_storage(config)
    cases = load_oracle_cases()
    report = await verify_cell(strategy, storage, cases)
    assert report.all_passed, format_report(report, cell_id)
