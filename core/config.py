"""Carga e validação de configuração de célula.

`cells/_defaults.yaml` guarda o que é comum a todas as células; cada
`cells/<id>.yaml` sobrepõe só o que muda. Falha alto (Pydantic,
`extra="forbid"`) em campo desconhecido — erro de digitação em nome de
campo não pode silenciosamente virar configuração padrão (ver
docs/ARCHITECTURE.md, "Configuração de célula").

Isto carrega e valida a configuração; a montagem real da célula (mapear
`strategy`/`storage` para as classes de strategies/ e storage/) é
responsabilidade de service/main.py, ainda não implementado.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

CELLS_DIR = Path("cells")


class CellParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n_candidates: int
    k: int
    prematerialized_m: int
    exclusion_size: int


class CellConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    strategy: str
    storage: str
    cache: Literal["none", "candidates", "response"] = "none"
    transport: Literal["http1", "http2", "grpc"] = "http1"
    params: CellParams
    storage_config: dict[str, str | int]


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_cell_config(cell_id: str, cells_dir: Path = CELLS_DIR) -> CellConfig:
    defaults_path = cells_dir / "_defaults.yaml"
    cell_path = cells_dir / f"{cell_id}.yaml"

    defaults = yaml.safe_load(defaults_path.read_text()) if defaults_path.exists() else {}
    overrides = yaml.safe_load(cell_path.read_text())

    merged = _deep_merge(defaults or {}, overrides or {})
    return CellConfig.model_validate(merged)
