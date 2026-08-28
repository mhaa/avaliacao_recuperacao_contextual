"""Exporta load/fixtures/contexts_by_tier.json a partir de
data_generation/data/contexts.parquet — reaproveita a seletividade e o
patamar (tier) já calculados na Etapa 3 (ver
data_generation/generator/contexts.py:select_tier_contexts /
assign_context_ids), sem recalcular nada.

load/scenarios.js usa este arquivo para escolher, por cenário, o único
context_id que corresponde à seletividade alvo (~2%/20%/60%, CONTEXTO.md
"Protocolo de medição") — mesma noção de "contexto único por patamar" que
data_generation/generator/oracle.py já usa para os casos do oráculo (ver
`tier_context_ids` em oracle.py:run).

Uso:
    docker compose run --rm --entrypoint python tools load/export_contexts_by_tier.py
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl

CONTEXTS_PATH = Path("data_generation/data/contexts.parquet")
OUTPUT_PATH = Path("load/fixtures/contexts_by_tier.json")


def build_tier_mapping(contexts_df: pl.DataFrame) -> dict[str, dict]:
    """Descarta os contextos "unused" (preenchimento até C=20, sem patamar
    de seletividade associado — ver contexts.py:assign_context_ids)."""
    viable = contexts_df.filter(pl.col("tier") != "unused")
    return {
        row["tier"]: {
            "context_id": row["context_id"],
            "candidate_selectivity": row["candidate_selectivity"],
        }
        for row in viable.iter_rows(named=True)
    }


def main() -> None:
    contexts_df = pl.read_parquet(CONTEXTS_PATH)
    mapping = build_tier_mapping(contexts_df)
    missing = {"high", "medium", "low"} - mapping.keys()
    if missing:
        raise ValueError(f"patamares ausentes em {CONTEXTS_PATH}: {sorted(missing)}")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(mapping, indent=2))
    print(f"Exportado: {OUTPUT_PATH} ({sorted(mapping)})")


if __name__ == "__main__":
    main()
