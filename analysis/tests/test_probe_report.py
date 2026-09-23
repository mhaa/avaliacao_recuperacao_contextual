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
    median_decision_summary,
    min_offered_ratio,
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


def test_ignore_latency_drops_only_the_p99_clause():
    # infra/scripts/run_stress_ramp.py:make_stress_probe_fn passa isto — a
    # campanha de estresse quer o teto do banco, não a SLO de latência de
    # cliente (docs/DESIGN.md, "Experimento complementar").
    over_latency = {"latency_ms_p99": 250.0, "error_rate": 0.0}
    assert violated_slo(over_latency, ignore_latency=True) is False

    over_error = {"latency_ms_p99": 50.0, "error_rate": 0.02}
    assert violated_slo(over_error, ignore_latency=True) is True


def test_ignore_latency_still_treats_missing_data_and_short_offered_load_as_violations():
    # Nem o portão de "nenhuma requisição parseada" nem o de vazão ofertada
    # são a SLO de latência — continuam valendo mesmo com ignore_latency=True.
    assert violated_slo({"latency_ms_p99": None, "error_rate": None}, ignore_latency=True) is True
    summary = {"latency_ms_p99": 50.0, "error_rate": 0.0}
    assert violated_slo(summary, offered_ratio=0.43, ignore_latency=True) is True


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


def test_main_ignore_latency_slo_flag_flips_a_latency_only_violation(tmp_path, monkeypatch, capsys):
    # infra/scripts/run_stress_ramp.py:make_stress_probe_fn passa
    # --ignore-latency-slo — só a campanha de estresse, nunca a bateria
    # principal. p99 acima do limiar, sem erro nenhum: sem a flag é
    # violated_slo=True; com ela, False.
    ndjson_path = tmp_path / "requests.ndjson"
    ndjson_path.write_text(_probe_request(250.0) + "\n")

    monkeypatch.setenv("GENERATOR_CPU_STAT_BEFORE", "cpu  0 0 0 0 0 0 0 0 0 0")
    monkeypatch.setenv("GENERATOR_CPU_STAT_AFTER", "cpu  0 0 0 0 0 0 0 0 0 0")

    exit_code = main([str(ndjson_path)])
    assert exit_code == 0
    assert "violated_slo=True" in capsys.readouterr().out

    exit_code = main(["--ignore-latency-slo", str(ndjson_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "violated_slo=False" in out
    assert "ignore_latency_slo=True" in out


def test_median_decision_summary_takes_the_median_p99_and_error_rate_across_reps():
    summaries = [
        {"latency_ms_p99": 100.0, "error_rate": 0.0},
        {"latency_ms_p99": 419.0, "error_rate": 0.02},
        {"latency_ms_p99": 200.0, "error_rate": 0.0},
        {"latency_ms_p99": 232.0, "error_rate": 0.0},
        {"latency_ms_p99": 50.0, "error_rate": 0.0},
    ]
    assert median_decision_summary(summaries) == {"latency_ms_p99": 200.0, "error_rate": 0.0}


def test_median_decision_summary_treats_an_empty_repetition_as_worst_case():
    # Repetição sem nenhuma requisição parseada (latency_ms_p99/error_rate
    # None, analysis/collect.py:build_summary) entra como pior caso na
    # mediana, nunca é descartada — mesma regra de violated_slo.
    summaries = [
        {"latency_ms_p99": 50.0, "error_rate": 0.0},
        {"latency_ms_p99": 60.0, "error_rate": 0.0},
        {"latency_ms_p99": None, "error_rate": None},
    ]
    result = median_decision_summary(summaries)
    assert result["latency_ms_p99"] == 60.0  # mediana de [50, 60, inf]
    assert result["error_rate"] == 0.0  # mediana de [0.0, 0.0, 1.0]


def test_min_offered_ratio_uses_the_worst_repetition_not_the_average():
    # 4 repetições saudáveis (ratio=1.0) + 1 degradada (ratio=0.5) — o
    # portão de vazão ofertada é de validade, não de desempenho: mediana
    # deixaria a degradada se esconder atrás das 4 saudáveis.
    summaries = [{"request_count": n} for n in (100, 100, 100, 100, 50)]
    assert min_offered_ratio(summaries, expected_requests_per_rep=100) == 0.5


def test_min_offered_ratio_is_none_without_expected_requests():
    assert min_offered_ratio([{"request_count": 100}], expected_requests_per_rep=None) is None


def _write_probe_ndjson(path, latencies_ms: list[float]) -> None:
    path.write_text("\n".join(_probe_request(latency) for latency in latencies_ms) + "\n")


def test_main_median_per_repetition_flags_a_violation_pooling_hides(tmp_path, monkeypatch, capsys):
    # Reconstrução do efeito achado ao vivo (e3-postgres, seletividade alta,
    # 4365 req/s — p99 individuais das 5 repetições todas > 200ms, mas o p99
    # do pool concatenado ficava <= 200ms): 3 repetições PEQUENAS e
    # degradadas (2% de requisições lentas cada, p99 próprio = 250ms, viola)
    # + 2 repetições GRANDES e saudáveis (só requisições rápidas, p99
    # próprio = 10ms). Poolizar tudo dilui as poucas requisições lentas das
    # 3 ruins (6 de 4150, ~0.14%) na massa das 2 boas — o p99 do pool fica
    # em 10ms (não viola), enquanto a MEDIANA dos 5 p99 individuais é
    # 250ms (viola).
    bad_latencies = [10.0] * 48 + [250.0] * 2
    good_latencies = [10.0] * 2000
    paths = []
    for i, latencies in enumerate(
        [bad_latencies, bad_latencies, bad_latencies, good_latencies, good_latencies]
    ):
        path = tmp_path / f"rep{i}.ndjson"
        _write_probe_ndjson(path, latencies)
        paths.append(str(path))

    monkeypatch.setenv("GENERATOR_CPU_STAT_BEFORE", "cpu  0 0 0 0 0 0 0 0 0 0")
    monkeypatch.setenv("GENERATOR_CPU_STAT_AFTER", "cpu  0 0 0 0 0 0 0 0 0 0")

    exit_code = main(["--decision-statistic", "pooled", *paths])
    assert exit_code == 0
    assert "violated_slo=False" in capsys.readouterr().out  # comportamento antigo preservado

    exit_code = main(["--decision-statistic", "median-per-repetition", *paths])
    assert exit_code == 0
    assert "violated_slo=True" in capsys.readouterr().out


def test_main_defaults_to_pooled_decision_statistic_for_backward_compat(tmp_path, monkeypatch, capsys):
    rep0 = tmp_path / "rep0.ndjson"
    rep0.write_text(_probe_request(10.0) + "\n")
    rep1 = tmp_path / "rep1.ndjson"
    rep1.write_text(_probe_request(300.0) + "\n")

    monkeypatch.setenv("GENERATOR_CPU_STAT_BEFORE", "cpu  0 0 0 0 0 0 0 0 0 0")
    monkeypatch.setenv("GENERATOR_CPU_STAT_AFTER", "cpu  0 0 0 0 0 0 0 0 0 0")

    main([str(rep0), str(rep1)])
    implicit_out = capsys.readouterr().out
    main(["--decision-statistic", "pooled", str(rep0), str(rep1)])
    explicit_out = capsys.readouterr().out

    assert implicit_out == explicit_out


def test_main_prints_per_rep_diagnostic_tokens_in_median_mode(tmp_path, monkeypatch, capsys):
    rep0 = tmp_path / "rep0.ndjson"
    rep0.write_text(_probe_request(10.0) + "\n")
    rep1 = tmp_path / "rep1.ndjson"
    rep1.write_text(_probe_request(300.0) + "\n")

    monkeypatch.setenv("GENERATOR_CPU_STAT_BEFORE", "cpu  0 0 0 0 0 0 0 0 0 0")
    monkeypatch.setenv("GENERATOR_CPU_STAT_AFTER", "cpu  0 0 0 0 0 0 0 0 0 0")

    exit_code = main(["--decision-statistic", "median-per-repetition", str(rep0), str(rep1)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "per_rep_p99_ms=10.0,300.0" in out
    assert "per_rep_violated=False,True" in out


def test_main_omits_per_rep_diagnostic_tokens_in_pooled_mode(tmp_path, monkeypatch, capsys):
    ndjson_path = tmp_path / "requests.ndjson"
    ndjson_path.write_text(_probe_request(10.0) + "\n")

    monkeypatch.setenv("GENERATOR_CPU_STAT_BEFORE", "cpu  0 0 0 0 0 0 0 0 0 0")
    monkeypatch.setenv("GENERATOR_CPU_STAT_AFTER", "cpu  0 0 0 0 0 0 0 0 0 0")

    exit_code = main([str(ndjson_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "per_rep_p99_ms" not in out
    assert "per_rep_violated" not in out
