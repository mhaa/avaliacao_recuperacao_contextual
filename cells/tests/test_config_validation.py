"""Valida cells/*.yaml: as 14 células reais carregam sem erro, e um campo
desconhecido (no topo ou dentro de `params`) levanta erro de validação em
vez de cair silenciosamente no default — erro de digitação em nome de
campo não pode virar configuração padrão sem avisar (ver IMPLEMENTACAO.md,
"Configuração de célula"). Suíte rápida, sem banco.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from core.config import load_cell_config

CELLS_DIR = Path("cells")

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
def test_real_cell_yaml_loads_without_error(cell_id):
    config = load_cell_config(cell_id, cells_dir=CELLS_DIR)
    assert config.id == cell_id


def test_unknown_top_level_field_is_typo_not_silently_defaulted(tmp_path):
    (tmp_path / "_defaults.yaml").write_text(
        "cache: none\n"
        "transport: http1\n"
        "params:\n"
        "  n_candidates: 500\n"
        "  k: 20\n"
        "  prematerialized_m: 40\n"
        "  exclusion_size: 20\n"
    )
    (tmp_path / "broken.yaml").write_text(
        "id: broken\n"
        "strategey: e1_app_filter\n"  # typo proposital
        "storage: postgres\n"
        "storage_config:\n"
        "  host: postgres\n"
        "  port: 5432\n"
    )
    with pytest.raises(ValidationError):
        load_cell_config("broken", cells_dir=tmp_path)


def test_unknown_field_inside_params_fails(tmp_path):
    (tmp_path / "_defaults.yaml").write_text("cache: none\ntransport: http1\n")
    (tmp_path / "broken.yaml").write_text(
        "id: broken\n"
        "strategy: e1_app_filter\n"
        "storage: postgres\n"
        "params:\n"
        "  n_candidates: 500\n"
        "  k: 20\n"
        "  prematerialized_m: 40\n"
        "  exclusion_size: 20\n"
        "  extra_typo_field: 1\n"
        "storage_config:\n"
        "  host: postgres\n"
        "  port: 5432\n"
    )
    with pytest.raises(ValidationError):
        load_cell_config("broken", cells_dir=tmp_path)


def test_defaults_merge_with_partial_override(tmp_path):
    (tmp_path / "_defaults.yaml").write_text(
        "cache: none\n"
        "transport: http1\n"
        "params:\n"
        "  n_candidates: 500\n"
        "  k: 20\n"
        "  prematerialized_m: 40\n"
        "  exclusion_size: 20\n"
    )
    (tmp_path / "custom.yaml").write_text(
        "id: custom\n"
        "strategy: e2_pushdown\n"
        "storage: valkey\n"
        "cache: response\n"  # só isso é sobrescrito
        "storage_config:\n"
        "  host: valkey\n"
        "  port: 6379\n"
    )
    config = load_cell_config("custom", cells_dir=tmp_path)
    assert config.cache == "response"
    assert config.transport == "http1"  # herdado do default
    assert config.params.k == 20  # herdado do default
