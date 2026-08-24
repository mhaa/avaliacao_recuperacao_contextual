"""Layout de arquivos de dados e verificação de idempotência entre etapas."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass(frozen=True)
class DataPaths:
    data_dir: Path

    @classmethod
    def default(cls) -> "DataPaths":
        return cls(DEFAULT_DATA_DIR)

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw" / "ml-32m"

    @property
    def id_maps(self) -> Path:
        return self.data_dir / "id_maps.parquet"

    @property
    def candidates_dir(self) -> Path:
        return self.data_dir / "candidates.parquet"

    @property
    def items(self) -> Path:
        return self.data_dir / "items.parquet"

    @property
    def contexts(self) -> Path:
        return self.data_dir / "contexts.parquet"

    @property
    def inverted_lists(self) -> Path:
        return self.data_dir / "inverted_lists.parquet"

    @property
    def inverted_bitmaps_dir(self) -> Path:
        return self.data_dir / "inverted_bitmaps"

    @property
    def prematerialized(self) -> Path:
        return self.data_dir / "prematerialized.parquet"

    @property
    def oracle(self) -> Path:
        return self.data_dir / "oracle.parquet"

    @property
    def stats(self) -> Path:
        return self.data_dir / "stats.json"

    @property
    def run_log(self) -> Path:
        return self.data_dir / "run.log"

    def synthetic_dir(self, scale: int) -> Path:
        return self.data_dir / "synthetic" / str(scale)


def read_stats(stats_path: Path) -> dict[str, Any]:
    if not stats_path.exists():
        return {}
    with stats_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def stage_is_done(
    stage: str,
    outputs: Iterable[Path],
    stats_path: Path,
    params: dict[str, Any],
) -> bool:
    """True se a etapa já rodou com os mesmos parâmetros e todas as saídas existem.

    Um parâmetro diferente (ex.: sample_users, seed) é tratado como stale
    mesmo sem --force, para nunca reaproveitar silenciosamente uma saída
    gerada em outra escala/config.
    """
    if not all(p.exists() for p in outputs):
        return False
    stats = read_stats(stats_path)
    recorded_params = stats.get(stage, {}).get("params")
    return recorded_params == params
