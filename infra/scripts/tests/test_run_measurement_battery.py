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
    _duration_seconds,
    _parse_probe_result,
    _write_saturation_json,
    build_remote_battery_command,
    build_remote_probe_command,
    build_remote_setup_command,
    build_remote_upload_command,
    build_sweep,
    sample_resources_periodically,
    shuffled_sweep,
)
from load.saturation import ProbeResult, SaturationSearchResult


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
        "20260101T000000Z", user_count=200_948,
    )
    assert "--rate 1000" in cmd
    assert "--selectivity-tier medium" in cmd
    assert "--phase triagem" in cmd
    assert "--repetitions 5" in cmd
    assert "--timestamp 20260101T000000Z" in cmd


def test_build_remote_battery_command_passes_user_count_to_run_battery():
    # docs/DESIGN.md, "Protocolo de medição": o Zipf do k6 amostra a base
    # INTEIRA carregada — sem --user-count, load/run_battery.py recusa (e o
    # default do zipf.js amostraria só 10.000 dos 200.948 usuários, o bug
    # que invalidou a primeira triagem).
    cmd = build_remote_battery_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "triagem",
        1000, "medium", 5, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "20260101T000000Z", user_count=200_948,
    )
    assert "--user-count 200948" in cmd


def test_build_remote_battery_command_mounts_results_dir():
    cmd = build_remote_battery_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "triagem",
        100, "medium", 1, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "20260101T000000Z", user_count=200_948,
    )
    assert "-v /home/tcc/results:/app/results" in cmd


def test_build_remote_battery_command_mounts_fixtures_dir_readonly():
    cmd = build_remote_battery_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "triagem",
        100, "medium", 1, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "20260101T000000Z", user_count=200_948,
    )
    assert "-v /home/tcc/load-fixtures:/app/load/fixtures:ro" in cmd


def test_build_remote_upload_command_uses_the_upload_script_with_no_gcloud_cli():
    # load/upload_results.py, não `gcloud storage cp` — a VM não tem gcloud
    # CLI (COS), só a imagem tools com google-cloud-storage instalado.
    cmd = build_remote_upload_command(
        "/app/results/e1-postgres", "/home/tcc/results", "my-results-bucket",
        "e1-postgres", "gcr.io/x/tools:1",
    )
    assert "load/upload_results.py" in cmd
    assert "gcloud" not in cmd


def test_build_remote_upload_command_passes_local_dir_bucket_and_prefix_as_argv():
    cmd = build_remote_upload_command(
        "/app/results/_saturation/e1-postgres", "/home/tcc/results", "my-results-bucket",
        "_saturation/e1-postgres", "gcr.io/x/tools:1",
    )
    assert cmd.endswith(
        "load/upload_results.py /app/results/_saturation/e1-postgres my-results-bucket "
        "_saturation/e1-postgres"
    )


def test_build_remote_upload_command_mounts_results_dir_and_uses_network_host():
    # --network host: obrigatório pra storage.Client() enxergar o metadata
    # server da VM (ADC) — ver docstring de load/upload_results.py.
    cmd = build_remote_upload_command(
        "/app/results/e1-postgres", "/home/tcc/results", "my-results-bucket",
        "e1-postgres", "gcr.io/x/tools:1",
    )
    assert "-v /home/tcc/results:/app/results" in cmd
    assert "--network host" in cmd


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


def test_build_remote_setup_command_skip_dataset_load_drops_schema_and_loader():
    # snapshot já semeado (infra/scripts/seed_dataset_snapshots.py) — nem
    # o schema nem load_full_dataset.py devem rodar de novo, só o passo
    # de contexto-por-seletividade, que nunca depende do banco.
    cmd = build_remote_setup_command(
        "e1-postgres", "postgres", "10.0.0.2", "10.0.0.3", "gcr.io/x/tools:1",
        "hunter2", "/home/tcc/load-fixtures", "my-project-tcc-dataset",
        skip_dataset_load=True,
    )
    assert "load_full_dataset.py" not in cmd
    assert "apply_schema.py" not in cmd
    assert "export_contexts_by_tier.py" in cmd


def test_duration_seconds_parses_the_k6_durations_this_orchestrator_uses():
    assert _duration_seconds("0s") == 0
    assert _duration_seconds("30s") == 30
    assert _duration_seconds("2m") == 120
    assert _duration_seconds("3m") == 180


def test_build_remote_probe_command_uses_probe_mode_and_rate():
    cmd = build_remote_probe_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "medium", 4000, "0s", "1m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "_saturation/e1-postgres/short-0-4000", 1, user_count=200_948,
    )
    assert "PROBE_MODE=true" in cmd
    assert "PROBE_RATE=4000" in cmd
    assert "analysis/probe_report.py" in cmd


def test_build_remote_probe_command_injects_user_count_and_expected_requests():
    # USER_COUNT: mesma razão da bateria de carga fixa (Zipf sobre a base
    # inteira). --expected-requests: taxa × medição × repetições — é o que
    # permite ao veredito da sondagem detectar o k6 descartando chegadas
    # (docs/DESIGN.md, "Vazão ofertada verificada, não presumida").
    cmd = build_remote_probe_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "low", 1000, "2m", "3m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "_saturation/e1-postgres/confirm-low-0-1000", 5, user_count=200_948,
    )
    assert "USER_COUNT=200948" in cmd
    assert f"--expected-requests {1000 * 180 * 5}" in cmd


def test_build_remote_probe_command_repeats_k6_for_each_repetition():
    cmd = build_remote_probe_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "low", 1000, "2m", "3m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "_saturation/e1-postgres/confirm-low-0-1000", 5, user_count=200_948,
    )
    assert cmd.count("PROBE_MODE=true") == 5
    for rep in range(5):
        assert f"rep{rep}/k6-raw.json" in cmd


def test_build_remote_probe_command_captures_proc_stat_before_and_after():
    # A CPU do gerador vem de /proc/stat lido na própria VM (não do Cloud
    # Monitoring) — as duas leituras precisam cercar a geração de carga e
    # ser repassadas para analysis/probe_report.py por variável de ambiente.
    cmd = build_remote_probe_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "medium", 4000, "0s", "1m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "_saturation/e1-postgres/short-0-4000", 1, user_count=200_948,
    )
    assert "cat /proc/stat" in cmd
    assert "GENERATOR_CPU_STAT_BEFORE=" in cmd
    assert "GENERATOR_CPU_STAT_AFTER=" in cmd
    before_idx = cmd.index("BEFORE_STAT=")
    probe_report_idx = cmd.index("analysis/probe_report.py")
    after_idx = cmd.index("AFTER_STAT=", before_idx + 1)
    assert before_idx < after_idx < probe_report_idx


def test_parse_probe_result_reads_violated_slo_true():
    stdout = (
        "algum log irrelevante\nPROBE_RESULT violated_slo=True p99=250.0 error_rate=0.0 "
        "request_count=1000 generator_cpu_percent=12.5\n"
    )
    assert _parse_probe_result(stdout).violated_slo is True


def test_parse_probe_result_reads_violated_slo_false():
    stdout = (
        "PROBE_RESULT violated_slo=False p99=50.0 error_rate=0.0 "
        "request_count=1000 generator_cpu_percent=12.5"
    )
    assert _parse_probe_result(stdout).violated_slo is False


def test_parse_probe_result_reads_p99_error_rate_and_generator_cpu():
    stdout = (
        "PROBE_RESULT violated_slo=True p99=250.0 error_rate=0.02 "
        "request_count=1000 generator_cpu_percent=45.2"
    )
    verdict = _parse_probe_result(stdout)
    assert verdict.p99_ms == 250.0
    assert verdict.error_rate == 0.02
    assert verdict.generator_cpu_percent == 45.2


def test_parse_probe_result_reads_p99_and_error_rate_as_none_when_literal_none():
    # analysis/probe_report.py imprime `p99=None`/`error_rate=None` (texto
    # literal do Python) quando nenhuma requisição foi parseada.
    stdout = (
        "PROBE_RESULT violated_slo=True p99=None error_rate=None "
        "request_count=0 generator_cpu_percent=0.0"
    )
    verdict = _parse_probe_result(stdout)
    assert verdict.p99_ms is None
    assert verdict.error_rate is None


def test_parse_probe_result_reads_offered_ratio_and_tolerates_its_absence():
    # Linha nova (com --expected-requests) traz offered_ratio; linhas de
    # execuções antigas não têm o token — precisa continuar parseável.
    with_ratio = (
        "PROBE_RESULT violated_slo=True p99=50.0 error_rate=0.0 "
        "request_count=90000 offered_ratio=0.43 generator_cpu_percent=20.0"
    )
    assert _parse_probe_result(with_ratio).offered_ratio == 0.43

    without_ratio = (
        "PROBE_RESULT violated_slo=False p99=50.0 error_rate=0.0 "
        "request_count=1000 generator_cpu_percent=20.0"
    )
    assert _parse_probe_result(without_ratio).offered_ratio is None


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
    assert payload["generator_cpu_unmeasured"] is False


def test_write_saturation_json_records_an_unmeasured_probe_as_null(tmp_path, monkeypatch):
    # Uma sondagem sem leitura de CPU precisa chegar ao arquivo como `null`,
    # não como 0.0 — é o que permite, meses depois, distinguir "gerador
    # ocioso" de "portão dos 60% nunca avaliado" numa medição arquivada.
    monkeypatch.chdir(tmp_path)
    result = SaturationSearchResult(
        approx_throughput=11000.0,
        censored=False,
        lower_bound=None,
        loadgen_bottleneck=False,
        generator_cpu_unmeasured=True,
        probes=[ProbeResult(rate=1000, violated_slo=False, generator_cpu_percent=None)],
    )
    _write_saturation_json(result, "e1-postgres", "triagem", "20260101T000000Z")

    out = tmp_path / "results" / "e1-postgres" / "triagem" / "20260101T000000Z" / "saturation.json"
    payload = json.loads(out.read_text())
    assert payload["generator_cpu_unmeasured"] is True
    assert payload["probes"][0]["generator_cpu_percent"] is None


def test_write_saturation_json_records_p99_and_error_rate_per_probe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = SaturationSearchResult(
        approx_throughput=1500.0,
        censored=False,
        lower_bound=None,
        loadgen_bottleneck=False,
        probes=[
            ProbeResult(
                rate=1000, violated_slo=False, generator_cpu_percent=12.5,
                p99_ms=95.0, error_rate=0.0,
            ),
            ProbeResult(
                rate=2000, violated_slo=True, generator_cpu_percent=30.0,
                p99_ms=250.0, error_rate=0.02,
            ),
        ],
    )
    _write_saturation_json(result, "e1-postgres", "triagem", "20260101T000000Z")

    out = tmp_path / "results" / "e1-postgres" / "triagem" / "20260101T000000Z" / "saturation.json"
    payload = json.loads(out.read_text())
    assert payload["probes"][0]["p99_ms"] == 95.0
    assert payload["probes"][0]["error_rate"] == 0.0
    assert payload["probes"][1]["p99_ms"] == 250.0
    assert payload["probes"][1]["error_rate"] == 0.02


def test_write_saturation_json_records_offered_ratio_per_probe(tmp_path, monkeypatch):
    # Auditoria da vazão ofertada (docs/DESIGN.md): distingue, no arquivo,
    # "violou o SLO" de "o k6 nem conseguiu ofertar o patamar". None em
    # sondagens antigas (sem --expected-requests).
    monkeypatch.chdir(tmp_path)
    result = SaturationSearchResult(
        approx_throughput=1000.0,
        censored=False,
        lower_bound=None,
        loadgen_bottleneck=False,
        probes=[
            ProbeResult(
                rate=2000, violated_slo=True, generator_cpu_percent=20.0,
                p99_ms=50.0, error_rate=0.0, offered_ratio=0.43,
            ),
            ProbeResult(rate=1000, violated_slo=False, generator_cpu_percent=10.0),
        ],
    )
    _write_saturation_json(result, "e1-postgres", "triagem", "20260101T000000Z")

    out = tmp_path / "results" / "e1-postgres" / "triagem" / "20260101T000000Z" / "saturation.json"
    payload = json.loads(out.read_text())
    assert payload["probes"][0]["offered_ratio"] == 0.43
    assert payload["probes"][1]["offered_ratio"] is None


def test_write_saturation_json_records_p99_and_error_rate_as_null_when_unmeasured(
    tmp_path, monkeypatch
):
    # Mesma sondagem sem nenhuma requisição parseada (analysis/probe_report.py
    # imprime p99=None/error_rate=None) — precisa chegar como `null`, não 0.0.
    monkeypatch.chdir(tmp_path)
    result = SaturationSearchResult(
        approx_throughput=None,
        censored=False,
        lower_bound=None,
        loadgen_bottleneck=False,
        probes=[
            ProbeResult(
                rate=1000, violated_slo=True, generator_cpu_percent=0.0,
                p99_ms=None, error_rate=None,
            ),
        ],
    )
    _write_saturation_json(result, "e1-postgres", "triagem", "20260101T000000Z")

    out = tmp_path / "results" / "e1-postgres" / "triagem" / "20260101T000000Z" / "saturation.json"
    payload = json.loads(out.read_text())
    assert payload["probes"][0]["p99_ms"] is None
    assert payload["probes"][0]["error_rate"] is None


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
