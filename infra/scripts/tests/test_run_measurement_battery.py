"""Testes das funções puras de infra/scripts/run_measurement_battery.py — a
orquestração real (terraform/gcloud/SSH) não é testada aqui, mesmo padrão de
load/tests/test_run_battery.py e a ausência de teste para
infra/scripts/cloud_smoke_test.py:main() (exige projeto GCP real)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from infra.scripts.run_measurement_battery import (
    FIXED_LOAD_LEVELS,
    LEGACY_HIGH_RATE_FALLBACK,
    SELECTIVITY_TIERS,
    TRIAGEM_RATE,
    TRIAGEM_TIER,
    _duration_seconds,
    _high_rate_from_saturation,
    _parse_probe_result,
    _write_saturation_json,
    build_final_level_confirmation_commands,
    build_remote_battery_command,
    build_remote_probe_aggregate_command,
    build_remote_probe_rep_command,
    build_remote_setup_command,
    build_remote_stat_capture_command,
    build_remote_upload_command,
    build_saturation_upload_cmd,
    build_sweep,
    sample_resources_periodically,
    shuffled_sweep,
)
from load.saturation import ProbeResult, SaturationSearchResult

# Vazão de saturação fictícia por seletividade — mesmo papel do
# high_rate_by_tier real de main() (vem das 3 rampas de confirmação), só que
# fixado aqui para os testes não dependerem de nenhuma medição.
_HIGH_RATE_BY_TIER = {"high": 4587, "medium": 3672, "low": 3317}


def test_build_sweep_triagem_is_a_single_mid_level_combination():
    assert build_sweep("triagem") == [(TRIAGEM_RATE, TRIAGEM_TIER)]


def test_build_sweep_confirmacao_is_the_full_cross_product():
    sweep = build_sweep("confirmacao", _HIGH_RATE_BY_TIER)
    assert len(sweep) == (len(FIXED_LOAD_LEVELS) + 1) * len(SELECTIVITY_TIERS)
    expected_fixed = {(rate, tier) for rate in FIXED_LOAD_LEVELS for tier in SELECTIVITY_TIERS}
    expected_high = {(_HIGH_RATE_BY_TIER[tier], tier) for tier in SELECTIVITY_TIERS}
    assert set(sweep) == expected_fixed | expected_high


def test_build_sweep_confirmacao_uses_the_per_tier_high_rate_not_a_fixed_value():
    # Bug real que este teste evita: um "10.000" fixo continuaria testando
    # direto a região de falha, ignorando a vazão de saturação medida.
    sweep = build_sweep("confirmacao", _HIGH_RATE_BY_TIER)
    for tier, high_rate in _HIGH_RATE_BY_TIER.items():
        assert (high_rate, tier) in sweep


def test_build_sweep_confirmacao_requires_high_rate_by_tier():
    with pytest.raises(ValueError):
        build_sweep("confirmacao")


def test_build_sweep_confirmacao_can_be_scoped_to_a_single_tier():
    # --tier medium: só as 3 combinações de seletividade média, nunca
    # high/low (docs/DESIGN.md, re-medição de seletividade média).
    sweep = build_sweep("confirmacao", _HIGH_RATE_BY_TIER, tiers=["medium"])
    assert len(sweep) == len(FIXED_LOAD_LEVELS) + 1
    assert all(tier == "medium" for _, tier in sweep)
    assert (_HIGH_RATE_BY_TIER["medium"], "medium") in sweep


def test_build_sweep_confirmacao_defaults_to_all_tiers_when_tiers_omitted():
    # Garantia explícita de que o novo parâmetro `tiers` não muda o default
    # (já coberto implicitamente pelos testes acima, que não passam tiers).
    with_default = build_sweep("confirmacao", _HIGH_RATE_BY_TIER)
    explicit_all = build_sweep("confirmacao", _HIGH_RATE_BY_TIER, tiers=SELECTIVITY_TIERS)
    assert set(with_default) == set(explicit_all)


def test_shuffled_sweep_is_deterministic_given_the_same_seed():
    sweep = build_sweep("confirmacao", _HIGH_RATE_BY_TIER)
    assert shuffled_sweep(sweep, seed=1) == shuffled_sweep(sweep, seed=1)


def test_shuffled_sweep_differs_across_seeds():
    sweep = build_sweep("confirmacao", _HIGH_RATE_BY_TIER)
    assert shuffled_sweep(sweep, seed=1) != shuffled_sweep(sweep, seed=2)


def test_shuffled_sweep_is_a_permutation_not_a_subset():
    sweep = build_sweep("confirmacao", _HIGH_RATE_BY_TIER)
    assert sorted(shuffled_sweep(sweep, seed=7)) == sorted(sweep)


def _fake_saturation(approx_throughput=None, lower_bound=None, loadgen_bottleneck=False):
    return SaturationSearchResult(
        approx_throughput=approx_throughput,
        censored=lower_bound is not None and approx_throughput is None,
        lower_bound=lower_bound,
        loadgen_bottleneck=loadgen_bottleneck,
        generator_cpu_unmeasured=False,
        probes=[],
    )


def test_high_rate_from_saturation_uses_approx_throughput_when_available():
    saturation = _fake_saturation(approx_throughput=3791.4)
    assert _high_rate_from_saturation(saturation, "high") == 3791


def test_high_rate_from_saturation_falls_back_to_lower_bound_when_censored():
    saturation = _fake_saturation(approx_throughput=None, lower_bound=50_000)
    assert _high_rate_from_saturation(saturation, "high") == 50_000


def test_high_rate_from_saturation_falls_back_to_legacy_rate_on_generator_bottleneck():
    saturation = _fake_saturation(approx_throughput=None, lower_bound=None, loadgen_bottleneck=True)
    assert _high_rate_from_saturation(saturation, "high") == LEGACY_HIGH_RATE_FALLBACK


def test_build_remote_battery_command_includes_rate_and_tier():
    cmd = build_remote_battery_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "triagem",
        1000, "medium", 3, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "20260101T000000Z", user_count=200_948,
    )
    assert "--rate 1000" in cmd
    assert "--selectivity-tier medium" in cmd
    assert "--phase triagem" in cmd
    assert "--timestamp 20260101T000000Z" in cmd


def test_build_remote_battery_command_runs_exactly_one_repetition_at_the_given_index():
    # Bug real evitado: --repetitions N encadeava as N repetições numa só
    # sessão SSH — uma queda de conexão no meio perdia todas. Agora cada
    # chamada roda 1 repetição específica (o chamador, main(), faz N
    # chamadas separadas via gcloud_ssh_with_retry).
    cmd = build_remote_battery_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "triagem",
        1000, "medium", 3, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "20260101T000000Z", user_count=200_948,
    )
    assert "--repetitions 1" in cmd
    assert "--repetition-index 3" in cmd


def test_build_remote_battery_command_passes_user_count_to_run_battery():
    # docs/DESIGN.md, "Protocolo de medição": o Zipf do k6 amostra a base
    # INTEIRA carregada — sem --user-count, load/run_battery.py recusa (e o
    # default do zipf.js amostraria só 10.000 dos 200.948 usuários, o bug
    # que invalidou a primeira triagem).
    cmd = build_remote_battery_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "triagem",
        1000, "medium", 0, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "20260101T000000Z", user_count=200_948,
    )
    assert "--user-count 200948" in cmd


def test_build_remote_battery_command_passes_results_bucket_to_run_battery():
    # Sem isto, load/run_battery.py:run_k6 não sabe pra onde subir cada
    # k6-raw.json individual e o deixa acumulado no disco da loadgen até o
    # upload final em bloco — o "no space left on device" que derrubou
    # e3-postgres.
    cmd = build_remote_battery_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "triagem",
        1000, "medium", 0, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "20260101T000000Z", results_bucket="my-results-bucket", user_count=200_948,
    )
    assert "--results-bucket my-results-bucket" in cmd


def test_build_remote_battery_command_omits_results_bucket_flag_when_absent():
    cmd = build_remote_battery_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "triagem",
        1000, "medium", 0, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "20260101T000000Z", user_count=200_948,
    )
    assert "--results-bucket" not in cmd


def test_build_remote_battery_command_mounts_results_dir():
    cmd = build_remote_battery_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "triagem",
        100, "medium", 0, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "20260101T000000Z", user_count=200_948,
    )
    assert "-v /home/tcc/results:/app/results" in cmd


def test_build_remote_battery_command_mounts_fixtures_dir_readonly():
    cmd = build_remote_battery_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "triagem",
        100, "medium", 0, "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
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


def test_build_saturation_upload_cmd_copies_the_host_local_file_to_the_matching_bucket_prefix():
    # saturation.json/saturation_<tier>.json são escritos só no host
    # orquestrador (_write_saturation_json) — nunca passam pela VM loadgen,
    # então build_remote_upload_command (que sobe /app/results/<cell> DA VM)
    # não os alcança. Regressão para a lacuna "local-only" (memória
    # saturation_json_never_uploaded_to_gcs).
    cmd = build_saturation_upload_cmd(
        "e3-postgres", "confirmacao", "20260101T000000Z", "saturation_medium.json",
        "my-results-bucket",
    )
    assert cmd == [
        "gcloud", "storage", "cp",
        str(Path("results") / "e3-postgres" / "confirmacao" / "20260101T000000Z" / "saturation_medium.json"),
        "gs://my-results-bucket/e3-postgres/confirmacao/20260101T000000Z/saturation_medium.json",
    ]


def test_build_remote_stat_capture_command_writes_proc_stat_to_the_matching_before_after_path():
    cmd = build_remote_stat_capture_command(
        "e3-postgres/confirmacao/20260101T000000Z/3980-medium", "before",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
    )
    assert "mkdir -p /app/results/e3-postgres/confirmacao/20260101T000000Z/3980-medium" in cmd
    assert (
        "cat /proc/stat | head -1 > "
        "/app/results/e3-postgres/confirmacao/20260101T000000Z/3980-medium/before_stat.txt"
    ) in cmd


def test_build_final_level_confirmation_commands_archives_reps_under_the_rate_tier_combo_dir():
    # O ponto central da otimização: as repetições do patamar aprovado
    # precisam cair no MESMO layout <cell>/<phase>/<timestamp>/<rate>-<tier>/
    # repN/ que a bateria de carga fixa usaria — é isso que permite pular a
    # combinação "alta" da bateria (--only-saturation) sem perder o dado.
    prep_commands, aggregate_command = build_final_level_confirmation_commands(
        "e3-postgres", "http://svc:8000/v1/recommendations", "confirmacao",
        3980, "medium", "20260101T000000Z", "gcr.io/x/tools:1",
        "/home/tcc/results", "/home/tcc/load-fixtures", "3m", "median-per-repetition", 5,
        region="us-east4", zone="us-east4-a", results_bucket="my-results-bucket",
        user_count=200_948,
    )
    assert len(prep_commands) == 7  # before-stat + 5 reps + after-stat
    assert "before_stat.txt" in prep_commands[0]
    assert "after_stat.txt" in prep_commands[-1]
    for rep, cmd in enumerate(prep_commands[1:6]):
        assert "load/run_battery.py" in cmd
        assert f"--repetition-index {rep}" in cmd
        assert "--rate 3980" in cmd
        assert "--selectivity-tier medium" in cmd
    assert "analysis/probe_report.py" in aggregate_command
    assert "--decision-statistic median-per-repetition" in aggregate_command
    for rep in range(5):
        assert (
            f"/app/results/e3-postgres/confirmacao/20260101T000000Z/3980-medium/rep{rep}/requests.ndjson"
            in aggregate_command
        )


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


def test_build_remote_probe_rep_command_uses_probe_mode_and_rate():
    cmd = build_remote_probe_rep_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "medium", 4000, "0s", "1m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "_saturation/e1-postgres/short-0-4000", 0,
        capture_before_stat=True, capture_after_stat=True, user_count=200_948,
    )
    assert "PROBE_MODE=true" in cmd
    assert "PROBE_RATE=4000" in cmd
    # A agregação (analysis/probe_report.py) roda numa sessão SSH separada
    # (build_remote_probe_aggregate_command) — nunca nesta.
    assert "analysis/probe_report.py" not in cmd


def test_build_remote_probe_rep_command_injects_user_count():
    # Mesma razão da bateria de carga fixa: o Zipf do k6 amostra a base
    # inteira carregada.
    cmd = build_remote_probe_rep_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "low", 1000, "2m", "3m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "_saturation/e1-postgres/confirm-low-0-1000", 0,
        capture_before_stat=True, capture_after_stat=False, user_count=200_948,
    )
    assert "USER_COUNT=200948" in cmd


def test_build_remote_probe_rep_command_targets_its_own_rep_dir():
    # Bug real evitado: antes, todas as repetições de uma sondagem rodavam
    # encadeadas numa sessão SSH só — uma queda de conexão no meio perdia a
    # sondagem inteira. Agora cada repetição é uma chamada isolada.
    for rep in range(5):
        cmd = build_remote_probe_rep_command(
            "e1-postgres", "http://svc:8000/v1/recommendations", "low", 1000, "2m", "3m",
            "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
            "_saturation/e1-postgres/confirm-low-0-1000", rep,
            capture_before_stat=(rep == 0), capture_after_stat=(rep == 4), user_count=200_948,
        )
        assert cmd.count("PROBE_MODE=true") == 1
        assert f"rep{rep}/k6-raw.json" in cmd
        for other in range(5):
            if other != rep:
                assert f"rep{other}/k6-raw.json" not in cmd


def test_build_remote_probe_rep_command_uploads_and_deletes_k6_raw_json():
    # Bug real: sem isto, cada sondagem da rampa fina de confirmação
    # (5 repetições, várias sondagens até convergir) deixa um k6-raw.json
    # de vários GB no disco da loadgen sem nunca limpar — encheu o disco
    # de 100GB e derrubou e3-postgres com "no space left on device".
    cmd = build_remote_probe_rep_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "low", 1000, "2m", "3m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "_saturation/e1-postgres/confirm-low-0-1000", 1,
        capture_before_stat=False, capture_after_stat=False,
        results_bucket="my-results-bucket", user_count=200_948,
    )
    rep_dir = "/app/results/_saturation/e1-postgres/confirm-low-0-1000/rep1"
    assert (
        f"load/upload_one_file.py {rep_dir}/k6-raw.json my-results-bucket "
        "_saturation/e1-postgres/confirm-low-0-1000/rep1/k6-raw.json" in cmd
    )
    assert f"rm -f {rep_dir}/k6-raw.json" in cmd


def test_build_remote_probe_rep_command_omits_upload_step_without_results_bucket():
    cmd = build_remote_probe_rep_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "low", 1000, "2m", "3m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "_saturation/e1-postgres/confirm-low-0-1000", 0,
        capture_before_stat=True, capture_after_stat=True, user_count=200_948,
    )
    assert "load/upload_one_file.py" not in cmd
    assert "rm -f" not in cmd


def test_build_remote_probe_rep_command_captures_proc_stat_only_when_asked():
    # A CPU do gerador vem de /proc/stat lido na própria VM (não do Cloud
    # Monitoring) — só a repetição marcada precisa gravar a leitura (em
    # arquivo, não variável de ambiente: sessões SSH separadas não
    # compartilham variáveis de shell entre si).
    cmd_first = build_remote_probe_rep_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "medium", 4000, "0s", "1m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "_saturation/e1-postgres/short-0-4000", 0,
        capture_before_stat=True, capture_after_stat=False, user_count=200_948,
    )
    assert "cat /proc/stat" in cmd_first
    assert "before_stat.txt" in cmd_first
    assert "after_stat.txt" not in cmd_first

    cmd_middle = build_remote_probe_rep_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "medium", 4000, "0s", "1m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "_saturation/e1-postgres/short-0-4000", 1,
        capture_before_stat=False, capture_after_stat=False, user_count=200_948,
    )
    assert "cat /proc/stat" not in cmd_middle


def test_build_remote_probe_rep_command_creates_probe_dir_before_writing_before_stat():
    # Bug real ao vivo contra e3-valkey (4ª tentativa): before_stat.txt mora
    # em remote_subdir (pai de rep_dir), então se o mkdir -p rodar DEPOIS da
    # captura, a repetição 0 falha sempre com "No such file or directory" —
    # determinístico, não uma queda de conexão (as 3 tentativas do retry
    # falhavam de forma idêntica).
    cmd = build_remote_probe_rep_command(
        "e1-postgres", "http://svc:8000/v1/recommendations", "high", 4261, "2m", "3m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        "_saturation/e1-postgres/confirm-high-0-4261", 0,
        capture_before_stat=True, capture_after_stat=False, user_count=200_948,
    )
    mkdir_idx = cmd.index("mkdir -p")
    before_stat_idx = cmd.index("before_stat.txt")
    assert mkdir_idx < before_stat_idx


def test_build_remote_probe_aggregate_command_reads_both_stat_files_before_probe_report():
    cmd = build_remote_probe_aggregate_command(
        "_saturation/e1-postgres/confirm-low-0-1000", 5, 1000, "3m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
    )
    assert "analysis/probe_report.py" in cmd
    assert "GENERATOR_CPU_STAT_BEFORE=" in cmd
    assert "GENERATOR_CPU_STAT_AFTER=" in cmd
    assert "before_stat.txt" in cmd
    assert "after_stat.txt" in cmd
    before_idx = cmd.index("BEFORE_STAT=")
    probe_report_idx = cmd.index("analysis/probe_report.py")
    after_idx = cmd.index("AFTER_STAT=", before_idx + 1)
    assert before_idx < after_idx < probe_report_idx


def test_build_remote_probe_aggregate_command_lists_every_repetitions_ndjson():
    cmd = build_remote_probe_aggregate_command(
        "_saturation/e1-postgres/confirm-low-0-1000", 3, 1000, "3m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
    )
    for rep in range(3):
        assert (
            f"/app/results/_saturation/e1-postgres/confirm-low-0-1000/rep{rep}/requests.ndjson"
            in cmd
        )


def test_build_remote_probe_aggregate_command_injects_expected_requests():
    # taxa × medição × repetições — é o que permite ao veredito da sondagem
    # detectar o k6 descartando chegadas (docs/DESIGN.md, "Vazão ofertada
    # verificada, não presumida").
    cmd = build_remote_probe_aggregate_command(
        "_saturation/e1-postgres/confirm-low-0-1000", 5, 1000, "3m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
    )
    assert f"--expected-requests {1000 * 180 * 5}" in cmd


def test_build_remote_probe_aggregate_command_defaults_to_pooled_decision_statistic():
    cmd = build_remote_probe_aggregate_command(
        "_saturation/e1-postgres/confirm-low-0-1000", 5, 1000, "3m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
    )
    assert "--decision-statistic pooled" in cmd


def test_build_remote_probe_aggregate_command_includes_requested_decision_statistic():
    # Sempre explícito no comando remoto (nunca depende do default do lado
    # do container) — docs/DESIGN.md, re-medição de seletividade média.
    cmd = build_remote_probe_aggregate_command(
        "_saturation/e1-postgres/confirm-medium-0-3672", 5, 3672, "3m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        decision_statistic="median-per-repetition",
    )
    assert "--decision-statistic median-per-repetition" in cmd


def test_build_remote_probe_aggregate_command_omits_ignore_latency_slo_by_default():
    # A bateria principal (triagem/confirmação) nunca deve receber esta
    # flag — só infra/scripts/run_stress_ramp.py:make_stress_probe_fn a
    # passa explicitamente. Regressão: default precisa continuar False.
    cmd = build_remote_probe_aggregate_command(
        "_saturation/e1-postgres/confirm-low-0-1000", 5, 1000, "3m",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
    )
    assert "--ignore-latency-slo" not in cmd


def test_build_remote_probe_aggregate_command_includes_ignore_latency_slo_when_requested():
    cmd = build_remote_probe_aggregate_command(
        "_estresse/e4-valkey/20260101T000000Z/probe/knee-0-1000", 1, 1000, "60s",
        "gcr.io/x/tools:1", "/home/tcc/results", "/home/tcc/load-fixtures",
        ignore_latency_slo=True,
    )
    assert "--ignore-latency-slo" in cmd


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


def test_parse_probe_result_reads_per_rep_diagnostic_fields_when_present():
    # Só presentes com --decision-statistic median-per-repetition
    # (analysis/probe_report.py) — vírgula como separador, sem espaços.
    stdout = (
        "PROBE_RESULT violated_slo=True p99=250.0 error_rate=0.0 request_count=250 "
        "generator_cpu_percent=20.0 per_rep_p99_ms=10.0,300.0 per_rep_violated=False,True"
    )
    verdict = _parse_probe_result(stdout)
    assert verdict.per_rep_p99_ms == [10.0, 300.0]
    assert verdict.per_rep_violated_slo == [False, True]


def test_parse_probe_result_defaults_per_rep_fields_to_none_when_absent():
    # Modo pooled (default) ou saída de execuções antigas — sem os dois
    # tokens novos, precisa continuar parseável.
    stdout = (
        "PROBE_RESULT violated_slo=False p99=50.0 error_rate=0.0 "
        "request_count=1000 generator_cpu_percent=20.0"
    )
    verdict = _parse_probe_result(stdout)
    assert verdict.per_rep_p99_ms is None
    assert verdict.per_rep_violated_slo is None


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
