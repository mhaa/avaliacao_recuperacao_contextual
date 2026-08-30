"""Testes das funções puras de infra/scripts/run_measurement_battery.py — a
orquestração real (terraform/gcloud/SSH) não é testada aqui, mesmo padrão de
load/tests/test_run_battery.py e a ausência de teste para
infra/scripts/cloud_smoke_test.py:main() (exige projeto GCP real)."""

from __future__ import annotations

from infra.scripts.run_measurement_battery import (
    RATES,
    SELECTIVITY_TIERS,
    TRIAGEM_RATE,
    TRIAGEM_TIER,
    build_remote_battery_command,
    build_remote_setup_command,
    build_sweep,
    shuffled_sweep,
)


def test_build_sweep_triagem_is_a_single_mid_level_combination():
    assert build_sweep("triagem") == [(TRIAGEM_RATE, TRIAGEM_TIER)]


def test_build_sweep_confirmacao_is_the_full_cross_product():
    sweep = build_sweep("confirmacao")
    assert len(sweep) == len(RATES) * len(SELECTIVITY_TIERS)
    assert set(sweep) == {(rate, tier) for rate in RATES for tier in SELECTIVITY_TIERS}


def test_shuffled_sweep_is_deterministic_given_the_same_seed():
    sweep = build_sweep("confirmacao")
    assert shuffled_sweep(sweep, seed=1) == shuffled_sweep(sweep, seed=1)


def test_shuffled_sweep_differs_across_seeds():
    sweep = build_sweep("confirmacao")
    assert shuffled_sweep(sweep, seed=1) != shuffled_sweep(sweep, seed=2)


def test_shuffled_sweep_is_a_permutation_not_a_subset():
    sweep = build_sweep("confirmacao")
    assert sorted(shuffled_sweep(sweep, seed=7)) == sorted(sweep)


def test_build_remote_battery_command_includes_rate_and_tier():
    cmd = build_remote_battery_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "triagem",
        1000, "medium", 5, False, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
    )
    assert "--rate 1000" in cmd
    assert "--selectivity-tier medium" in cmd
    assert "--phase triagem" in cmd
    assert "--repetitions 5" in cmd
    assert "--ramp" not in cmd


def test_build_remote_battery_command_ramp_flag():
    cmd = build_remote_battery_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "triagem",
        100, "medium", 1, True, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
    )
    assert "--ramp" in cmd


def test_build_remote_battery_command_mounts_results_dir():
    cmd = build_remote_battery_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "triagem",
        100, "medium", 1, False, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
    )
    assert "-v /home/tcc/results:/app/results" in cmd


def test_build_remote_battery_command_mounts_fixtures_dir_readonly():
    cmd = build_remote_battery_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "triagem",
        100, "medium", 1, False, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
    )
    assert "-v /home/tcc/load-fixtures:/app/load/fixtures:ro" in cmd


def test_build_remote_setup_command_uses_the_full_loader_not_the_oracle_fixture():
    # load/zipf.js amostra de toda a população real — carregar só o
    # subconjunto do oráculo aqui corromperia silenciosamente a medição.
    cmd = build_remote_setup_command(
        "e1-postgres", "postgres", "10.0.0.2", "10.0.0.3", "gcr.io/x/tools:1",
        "hunter2", "/home/tcc/load-fixtures", "my-project-tcc-dataset",
    )
    assert "load_full_dataset.py" in cmd
    assert "load_oracle_fixture.py" not in cmd


def test_build_remote_setup_command_passes_dataset_bucket_env():
    cmd = build_remote_setup_command(
        "e1-postgres", "postgres", "10.0.0.2", "10.0.0.3", "gcr.io/x/tools:1",
        "hunter2", "/home/tcc/load-fixtures", "my-project-tcc-dataset",
    )
    assert "DATASET_BUCKET=my-project-tcc-dataset" in cmd
