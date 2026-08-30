"""Testes das funções puras de infra/scripts/run_measurement_battery.py — a
orquestração real (terraform/gcloud/SSH) não é testada aqui, mesmo padrão de
load/tests/test_run_battery.py e a ausência de teste para
infra/scripts/cloud_smoke_test.py:main() (exige projeto GCP real)."""

from __future__ import annotations

import json
import threading

from infra.scripts.run_measurement_battery import (
    RATES,
    SELECTIVITY_TIERS,
    TRIAGEM_RATE,
    TRIAGEM_TIER,
    _parse_probe_result_line,
    _write_saturation_json,
    build_remote_battery_command,
    build_remote_probe_command,
    build_remote_setup_command,
    build_sweep,
    sample_resources_periodically,
    shuffled_sweep,
)
from load.saturation import SaturationSearchResult


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
        1000, "medium", 5, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "20260101T000000Z",
    )
    assert "--rate 1000" in cmd
    assert "--selectivity-tier medium" in cmd
    assert "--phase triagem" in cmd
    assert "--repetitions 5" in cmd
    assert "--timestamp 20260101T000000Z" in cmd


def test_build_remote_battery_command_mounts_results_dir():
    cmd = build_remote_battery_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "triagem",
        100, "medium", 1, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "20260101T000000Z",
    )
    assert "-v /home/tcc/results:/app/results" in cmd


def test_build_remote_battery_command_mounts_fixtures_dir_readonly():
    cmd = build_remote_battery_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "triagem",
        100, "medium", 1, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "20260101T000000Z",
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


def test_build_remote_probe_command_uses_probe_mode_and_rate():
    cmd = build_remote_probe_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "medium", 4000, "0s", "1m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "_saturation/e1-postgres/short-0-4000", 1,
    )
    assert "PROBE_MODE=true" in cmd
    assert "PROBE_RATE=4000" in cmd
    assert "analysis/probe_report.py" in cmd


def test_build_remote_probe_command_repeats_k6_for_each_repetition():
    cmd = build_remote_probe_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "low", 1000, "2m", "3m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "_saturation/e1-postgres/confirm-low-0-1000", 5,
    )
    assert cmd.count("PROBE_MODE=true") == 5
    for rep in range(5):
        assert f"rep{rep}/k6-raw.json" in cmd


def test_parse_probe_result_line_reads_violated_slo_true():
    stdout = "algum log irrelevante\nPROBE_RESULT violated_slo=True p99=250.0 error_rate=0.0\n"
    assert _parse_probe_result_line(stdout) is True


def test_parse_probe_result_line_reads_violated_slo_false():
    stdout = "PROBE_RESULT violated_slo=False p99=50.0 error_rate=0.0"
    assert _parse_probe_result_line(stdout) is False


def test_write_saturation_json_round_trips(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = SaturationSearchResult(
        approx_throughput=11000.0, censored=False, lower_bound=None, loadgen_bottleneck=False
    )
    _write_saturation_json(result, "e1-postgres", "triagem", "20260101T000000Z")

    out = tmp_path / "results" / "e1-postgres" / "triagem" / "20260101T000000Z" / "saturation.json"
    payload = json.loads(out.read_text())
    assert payload["approx_throughput"] == 11000.0
    assert payload["censored"] is False


def test_sample_resources_periodically_stops_when_event_is_set():
    stop_event = threading.Event()
    calls = []

    def fake_collect_fn():
        calls.append(1)
        if len(calls) >= 3:
            stop_event.set()
        return [{"component": "database", "cpu_percent": 10.0}]

    samples_out: list = []
    sample_resources_periodically(fake_collect_fn, stop_event, samples_out, interval_seconds=0)

    assert len(calls) == 3
    assert len(samples_out) == 3


def test_sample_resources_periodically_tolerates_a_failing_collect_fn():
    stop_event = threading.Event()
    calls = []

    def flaky_collect_fn():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Cloud Monitoring indisponível")
        stop_event.set()
        return []

    samples_out: list = []
    sample_resources_periodically(flaky_collect_fn, stop_event, samples_out, interval_seconds=0)

    assert len(calls) == 2  # a exceção não travou o loop


def test_sample_resources_periodically_never_calls_when_already_stopped():
    stop_event = threading.Event()
    stop_event.set()
    calls = []

    def fake_collect_fn():
        calls.append(1)
        return []

    sample_resources_periodically(fake_collect_fn, stop_event, [], interval_seconds=0)

    assert calls == []
