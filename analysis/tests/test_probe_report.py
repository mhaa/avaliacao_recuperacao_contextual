"""Testa analysis/probe_report.py:violated_slo — docs/DESIGN.md, "Protocolo de
medição": "SLO: p99 > 200 ms ou taxa de erro > 1%." — e a leitura de CPU do
gerador via /proc/stat (_parse_proc_stat_cpu_fields/_cpu_percent_from_stat),
que substituiu a consulta ao Cloud Monitoring para este portão."""

from __future__ import annotations

import json

from analysis.probe_report import (
    _cpu_percent_from_stat,
    _parse_proc_stat_cpu_fields,
    main,
    violated_slo,
)


def test_violated_slo_false_when_within_both_thresholds():
    assert violated_slo({"latency_ms_p99": 150.0, "error_rate": 0.005}) is False


def test_violated_slo_true_when_latency_exceeds_threshold():
    assert violated_slo({"latency_ms_p99": 250.0, "error_rate": 0.0}) is True


def test_violated_slo_true_when_error_rate_exceeds_threshold():
    assert violated_slo({"latency_ms_p99": 50.0, "error_rate": 0.02}) is True


def test_violated_slo_true_when_no_requests_were_parsed():
    assert violated_slo({"latency_ms_p99": None, "error_rate": None}) is True


def test_violated_slo_true_when_offered_load_fell_short():
    # docs/DESIGN.md, "Vazão ofertada verificada, não presumida": p99/erro
    # dentro do SLO mas o k6 descartou chegadas (maxVUs esgotado) — o
    # patamar não foi de fato oferecido, conta como violação.
    summary = {"latency_ms_p99": 50.0, "error_rate": 0.0}
    assert violated_slo(summary, offered_ratio=0.43) is True


def test_violated_slo_ignores_offered_ratio_when_unknown_or_sufficient():
    summary = {"latency_ms_p99": 50.0, "error_rate": 0.0}
    assert violated_slo(summary, offered_ratio=None) is False
    assert violated_slo(summary, offered_ratio=0.99) is False


def test_parse_proc_stat_cpu_fields_reads_the_first_8_jiffie_counters():
    # Formato real de /proc/stat: "cpu" seguido de espaço duplo, depois
    # user nice system idle iowait irq softirq steal guest guest_nice.
    line = "cpu  100 0 100 800 0 0 0 0 0 0"
    assert _parse_proc_stat_cpu_fields(line) == (100, 0, 100, 800, 0, 0, 0, 0)


def test_parse_proc_stat_cpu_fields_rejects_a_non_cpu_line():
    try:
        _parse_proc_stat_cpu_fields("cpu0 50 0 50 400 0 0 0 0 0 0")
        assert False, "deveria ter levantado RuntimeError"
    except RuntimeError:
        pass


def test_cpu_percent_from_stat_is_100_when_all_delta_is_non_idle():
    before = (100, 0, 100, 800, 0, 0, 0, 0)
    after = (200, 0, 200, 800, 0, 0, 0, 0)  # +200 non-idle, idle parado
    assert _cpu_percent_from_stat(before, after) == 100.0


def test_cpu_percent_from_stat_is_0_when_all_delta_is_idle():
    before = (100, 0, 100, 800, 0, 0, 0, 0)
    after = (100, 0, 100, 900, 0, 0, 0, 0)  # só idle avançou
    assert _cpu_percent_from_stat(before, after) == 0.0


def test_cpu_percent_from_stat_is_50_when_half_the_delta_is_idle():
    before = (0, 0, 0, 0, 0, 0, 0, 0)
    after = (50, 0, 0, 50, 0, 0, 0, 0)  # 50 non-idle, 50 idle
    assert _cpu_percent_from_stat(before, after) == 50.0


def _probe_request(latency_ms: float, status: int = 200) -> str:
    return json.dumps(
        {
            "request_id": "0-0",
            "scenario": "probe",
            "timestamp": "2026-01-01T00:00:00.000Z",
            "latency_ms": latency_ms,
            "status": status,
            "returned_count": 20,
        }
    )


def test_main_prints_generator_cpu_percent_from_proc_stat_env_vars(tmp_path, monkeypatch, capsys):
    ndjson_path = tmp_path / "requests.ndjson"
    ndjson_path.write_text(_probe_request(10.0) + "\n")

    monkeypatch.setenv("GENERATOR_CPU_STAT_BEFORE", "cpu  100 0 100 800 0 0 0 0 0 0")
    monkeypatch.setenv("GENERATOR_CPU_STAT_AFTER", "cpu  200 0 200 800 0 0 0 0 0 0")

    exit_code = main([str(ndjson_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "PROBE_RESULT" in out
    assert "generator_cpu_percent=100.0" in out


def test_main_consolidates_multiple_ndjson_paths_into_one_verdict(tmp_path, monkeypatch, capsys):
    # rampa de confirmação: CONFIRMATION_REPETITIONS=5 requests.ndjson, um
    # por repetição — todos precisam entrar no mesmo cálculo de p99/
    # error_rate, não só o primeiro (bug real: argparse sem nargs="+"
    # rejeitava os paths extras com "unrecognized arguments").
    rep0 = tmp_path / "rep0.ndjson"
    rep0.write_text(_probe_request(10.0) + "\n")
    rep1 = tmp_path / "rep1.ndjson"
    rep1.write_text(_probe_request(20.0) + "\n")

    monkeypatch.setenv("GENERATOR_CPU_STAT_BEFORE", "cpu  0 0 0 0 0 0 0 0 0 0")
    monkeypatch.setenv("GENERATOR_CPU_STAT_AFTER", "cpu  0 0 0 0 0 0 0 0 0 0")

    exit_code = main([str(rep0), str(rep1)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "request_count=2" in out


def test_main_expected_requests_shortfall_flips_the_verdict(tmp_path, monkeypatch, capsys):
    # 1 requisição registrada de 10 esperadas, latência ótima: sem o portão
    # de vazão ofertada o veredito seria "passou o SLO" — com ele, é
    # violated_slo=True (o patamar não foi de fato oferecido).
    ndjson_path = tmp_path / "requests.ndjson"
    ndjson_path.write_text(_probe_request(10.0) + "\n")

    monkeypatch.setenv("GENERATOR_CPU_STAT_BEFORE", "cpu  0 0 0 0 0 0 0 0 0 0")
    monkeypatch.setenv("GENERATOR_CPU_STAT_AFTER", "cpu  0 0 0 0 0 0 0 0 0 0")

    exit_code = main(["--expected-requests", "10", str(ndjson_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "violated_slo=True" in out
    assert "offered_ratio=0.1" in out


def test_main_without_expected_requests_reports_offered_ratio_none(tmp_path, monkeypatch, capsys):
    # Compatibilidade com invocações antigas: sem --expected-requests o
    # portão não é avaliado (offered_ratio=None) e o SLO decide sozinho.
    ndjson_path = tmp_path / "requests.ndjson"
    ndjson_path.write_text(_probe_request(10.0) + "\n")

    monkeypatch.setenv("GENERATOR_CPU_STAT_BEFORE", "cpu  0 0 0 0 0 0 0 0 0 0")
    monkeypatch.setenv("GENERATOR_CPU_STAT_AFTER", "cpu  0 0 0 0 0 0 0 0 0 0")

    exit_code = main([str(ndjson_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "violated_slo=False" in out
    assert "offered_ratio=None" in out
