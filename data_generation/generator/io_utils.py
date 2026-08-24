"""Escrita determinística de artefatos: parquet ordenado e stats.json sem timestamps.

stats.json é comparado byte a byte no teste de reprodutibilidade (critério de
aceitação 2) — nunca gravar timestamps de parede ou durações nele. Métricas de
tempo de execução, se necessárias para o texto do TCC, vão em run.log, fora
dessa comparação.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl

from . import config


def write_parquet_deterministic(
    df: pl.DataFrame, path: Path, sort_by: list[str]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.sort(sort_by).write_parquet(
        path, compression=config.PARQUET_COMPRESSION
    )


def update_stats(stats_path: Path, section: str, data: dict[str, Any]) -> None:
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    stats: dict[str, Any] = {}
    if stats_path.exists():
        with stats_path.open("r", encoding="utf-8") as f:
            stats = json.load(f)
    stats[section] = data
    with stats_path.open("w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def append_run_log(run_log_path: Path, message: str) -> None:
    """Log de tempo de execução etc. — fora da comparação de reprodutibilidade."""
    run_log_path.parent.mkdir(parents=True, exist_ok=True)
    with run_log_path.open("a", encoding="utf-8") as f:
        f.write(message.rstrip("\n") + "\n")
