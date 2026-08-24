"""Critérios de aceitação da spec (data_generation/README.md), verificados em
escala de fixture (rápido, sem rede). A verificação em escala real
(--sample-users 10000 contra o MovieLens 32M baixado) fica para a suíte
`@pytest.mark.integration`, à parte.
"""

import filecmp
import subprocess
import sys
from pathlib import Path

import polars as pl
import pytest

from generator import cli
from generator.paths import DataPaths, read_stats
from tests.conftest import _make_fixture

_DATA_GENERATION_ROOT = Path(__file__).resolve().parent.parent


def _run_all(data_dir: DataPaths, seed: int = 42) -> None:
    cli.main(["all", "--data-dir", str(data_dir.data_dir), "--seed", str(seed)])


def test_full_pipeline_runs_without_error(medium_data_dir):
    _run_all(medium_data_dir)  # não deve levantar exceção

    for path in (
        medium_data_dir.id_maps,
        medium_data_dir.candidates_dir,
        medium_data_dir.items,
        medium_data_dir.contexts,
        medium_data_dir.inverted_lists,
        medium_data_dir.inverted_bitmaps_dir,
        medium_data_dir.prematerialized,
        medium_data_dir.oracle,
        medium_data_dir.stats,
    ):
        assert path.exists()


def test_stats_json_has_all_required_numbers(medium_data_dir):
    _run_all(medium_data_dir)
    stats = read_stats(medium_data_dir.stats)

    assert set(stats.keys()) >= {"ingest", "rank", "contexts", "artifacts", "oracle"}

    assert stats["ingest"]["U"] > 0
    assert stats["ingest"]["I"] > 0

    assert "als" in stats["rank"]
    assert stats["rank"]["total_candidate_rows"] > 0

    assert stats["contexts"]["tiers"].keys() == {"high", "medium", "low"}
    assert len(stats["contexts"]["c_contexts"]) > 0

    assert stats["artifacts"]["prematerialized_rows"] == stats["artifacts"]["prematerialized_expected_rows"]

    assert stats["oracle"]["case_count"] > 0
    assert stats["oracle"]["short_result_case_count"] >= 0


def _all_artifact_files(data_dir: DataPaths) -> list[Path]:
    return sorted(p for p in data_dir.data_dir.rglob("*") if p.is_file() and p.name != "run.log")


def _run_all_subprocess(data_dir: DataPaths, seed: int = 42) -> None:
    """Roda 'python -m generator all' como processo separado — cada um com
    pool de threads do polars limpo, para validar reprodutibilidade do jeito
    que ela realmente é garantida (POLARS_MAX_THREADS fixado em __main__.py
    antes de qualquer import que toque polars)."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "generator",
            "all",
            "--data-dir",
            str(data_dir.data_dir),
            "--seed",
            str(seed),
        ],
        cwd=_DATA_GENERATION_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_rerun_with_same_seed_is_byte_identical(tmp_path):
    dir_a = DataPaths(tmp_path / "run_a" / "data")
    dir_b = DataPaths(tmp_path / "run_b" / "data")
    _make_fixture(dir_a.raw_dir, n_users=200, n_movies=250, seed=7, ratings_per_user=30)
    _make_fixture(dir_b.raw_dir, n_users=200, n_movies=250, seed=7, ratings_per_user=30)

    _run_all_subprocess(dir_a, seed=42)
    _run_all_subprocess(dir_b, seed=42)

    files_a = _all_artifact_files(dir_a)
    files_b = _all_artifact_files(dir_b)
    rel_a = [p.relative_to(dir_a.data_dir) for p in files_a]
    rel_b = [p.relative_to(dir_b.data_dir) for p in files_b]
    assert rel_a == rel_b

    for ra in rel_a:
        assert filecmp.cmp(dir_a.data_dir / ra, dir_b.data_dir / ra, shallow=False), (
            f"artefato diferente entre execuções: {ra}"
        )


@pytest.mark.integration
def test_full_pipeline_runs_at_dev_sample_scale():
    """Critério 1 em condição real: requer o MovieLens 32M já baixado em
    data_generation/data/raw/ml-32m/."""
    data_dir = DataPaths.default()
    cli.main(["all", "--sample-users", "10000", "--seed", "42", "--data-dir", str(data_dir.data_dir)])
    assert data_dir.oracle.exists()
