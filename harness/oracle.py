"""Carrega data_generation/data/oracle.parquet — 1000 casos com resultado
esperado, computado de forma independente e ingênua sobre candidates.parquet
(ver data_generation/generator/oracle.py). Este módulo só lê; nunca
reimplementa a lógica de filtragem — é exatamente o que ele existe para
verificar, não para recalcular.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import polars as pl

DEFAULT_ORACLE_PATH = Path("data_generation/data/oracle.parquet")
EXPECTED_CASE_COUNT = 1000


@dataclass(frozen=True)
class OracleCase:
    case_id: int
    user_id: int
    context_ids: list[int]
    exclude_ids: list[int]
    k: int
    expected_item_ids: list[int]
    expected_scores: list[float]


def load_oracle_cases(path: Path = DEFAULT_ORACLE_PATH) -> list[OracleCase]:
    df = pl.read_parquet(path)
    return [
        OracleCase(
            case_id=row["case_id"],
            user_id=row["user_id"],
            context_ids=list(row["context_ids"]),
            exclude_ids=list(row["exclude_ids"]),
            k=row["k"],
            expected_item_ids=list(row["expected_item_ids"]),
            expected_scores=list(row["expected_scores"]),
        )
        for row in df.iter_rows(named=True)
    ]
