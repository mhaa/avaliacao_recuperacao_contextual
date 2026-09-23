"""Testes de analysis/ramp_report.py — agregação por degrau da rampa de
estresse. NDJSON sintético em tmp_path; nenhuma rede, gcloud ou k6."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from analysis.collect import RAMP_EXTRA_FIELDS, RAMP_SCENARIOS, parse_requests_ndjson
from analysis.ramp_report import build_ramp_report, knee_summary, main, step_results
from load.ramp import RampStepResult

BASE = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)


def _write_ndjson(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def _step_events(rate, phase, *, count, latency_ms, start_offset_s, status=200, span_s=60):
    """`count` requisições espalhadas por `span_s`, para o span do grupo — e
    portanto throughput_rps = n/span — ser controlável no teste."""
    return [
        {
            "scenario": "stress_ramp",
            "step_rate": rate,
            "step_phase": phase,
            "timestamp": (
                BASE + timedelta(seconds=start_offset_s + i * span_s / max(count - 1, 1))
            ).isoformat(),
            "latency_ms": latency_ms,
            "status": status,
            "returned_count": 20,
        }
        for i in range(count)
    ]


def test_parser_keeps_the_step_columns_only_when_asked(tmp_path):
    path = tmp_path / "requests.ndjson"
    _write_ndjson(path, _step_events(1000, "fine_up", count=10, latency_ms=3, start_offset_s=0))

    with_steps = parse_requests_ndjson(
        path, scenarios=RAMP_SCENARIOS, extra_fields=RAMP_EXTRA_FIELDS
    )
    without = parse_requests_ndjson(path, scenarios=RAMP_SCENARIOS)

    assert "step_rate" in with_steps.columns
    # O schema padrão de 4 colunas é o formato já gravado em todos os
    # latencies.parquet existentes — não pode mudar.
    assert without.columns == ["timestamp", "latency_ms", "status", "returned_count"]


def test_each_step_carries_all_four_percentiles(tmp_path):
    path = tmp_path / "requests.ndjson"
    _write_ndjson(path, _step_events(1000, "fine_up", count=100, latency_ms=5, start_offset_s=0))

    report = build_ramp_report(path, cell_id="e2-scylla", tier="medium")

    assert len(report["steps"]) == 1
    step = report["steps"][0]
    for key in ("latency_ms_p50", "latency_ms_p95", "latency_ms_p99", "latency_ms_p999"):
        assert step[key] is not None, key


def test_same_rate_on_ascent_and_descent_stays_separate(tmp_path):
    # Agrupar só por taxa fundiria os dois lados da histerese num número só —
    # exatamente o que o experimento quer distinguir.
    path = tmp_path / "requests.ndjson"
    _write_ndjson(
        path,
        _step_events(4000, "fine_up", count=60, latency_ms=3, start_offset_s=0)
        + _step_events(4000, "fine_down", count=60, latency_ms=900, start_offset_s=600),
    )

    report = build_ramp_report(path, cell_id="e4-valkey", tier="medium")

    assert len(report["steps"]) == 2
    phases = [s["phase"] for s in report["steps"]]
    assert phases == ["fine_up", "fine_down"]
    assert report["steps"][0]["latency_ms_p99"] < report["steps"][1]["latency_ms_p99"]


def test_steps_are_ordered_by_ramp_trajectory_not_by_rate(tmp_path):
    path = tmp_path / "requests.ndjson"
    _write_ndjson(
        path,
        _step_events(1000, "coarse_up", count=30, latency_ms=3, start_offset_s=0)
        + _step_events(5000, "fine_up", count=30, latency_ms=3, start_offset_s=100)
        + _step_events(5000, "plateau", count=30, latency_ms=50, start_offset_s=200)
        + _step_events(3000, "fine_down", count=30, latency_ms=4, start_offset_s=400),
    )

    report = build_ramp_report(path, cell_id="e2-scylla", tier="medium")

    assert [s["phase"] for s in report["steps"]] == [
        "coarse_up",
        "fine_up",
        "plateau",
        "fine_down",
    ]


def test_offered_ratio_is_recorded_per_step(tmp_path):
    # 61 requisições em 60 s contra um alvo de 1000 req/s: déficit enorme de
    # oferta, que é o regime esperado além do joelho.
    path = tmp_path / "requests.ndjson"
    _write_ndjson(path, _step_events(1000, "fine_up", count=61, latency_ms=3, start_offset_s=0))

    report = build_ramp_report(path, cell_id="e2-scylla", tier="medium")
    step = report["steps"][0]

    assert step["offered_ratio"] == pytest.approx(1.0 / 1000, rel=0.1)
    assert step["offered_load_ok"] is False
    # Déficit de oferta conta como violação — mesma regra da bateria
    # principal, não uma segunda implementação.
    assert step["violated_slo"] is True


def test_slo_throughput_rps_counts_only_fast_successful_requests(tmp_path):
    # 4 requisições num degrau de 10s: 2 dentro do SLO (rápida + sucesso),
    # 1 lenta (fora do SLO por latência), 1 rápida mas com erro (fora do SLO
    # por status) — goodput = 2/10 = 0,2 req/s, distinto do throughput_rps
    # bruto (4/10 = 0,4), que conta toda requisição aceita.
    path = tmp_path / "requests.ndjson"
    events = [
        {
            "scenario": "stress_ramp",
            "step_rate": 1000,
            "step_phase": "fine_up",
            "timestamp": (BASE + timedelta(seconds=offset)).isoformat(),
            "latency_ms": latency_ms,
            "status": status,
            "returned_count": 20,
        }
        for offset, latency_ms, status in [
            (0, 50, 200),
            (2, 250, 200),  # lenta — fora do SLO
            (4, 50, 500),  # erro — fora do SLO
            (10, 50, 200),
        ]
    ]
    _write_ndjson(path, events)

    report = build_ramp_report(path, cell_id="e4-valkey", tier="medium")
    step = report["steps"][0]

    assert step["slo_throughput_rps"] == pytest.approx(0.2)
    assert step["throughput_rps"] == pytest.approx(0.4)


def test_degraded_steps_keep_their_percentiles_instead_of_being_dropped(tmp_path):
    # Os degraus além do joelho são os interessantes: precisam sobreviver ao
    # relatório, marcados, nunca descartados.
    path = tmp_path / "requests.ndjson"
    _write_ndjson(path, _step_events(9000, "fine_up", count=61, latency_ms=4000, start_offset_s=0))

    report = build_ramp_report(path, cell_id="e2-scylla", tier="medium")
    step = report["steps"][0]

    assert step["offered_load_ok"] is False
    assert step["latency_ms_p99"] is not None
    assert step["request_count"] == 61


def test_step_timestamps_are_present_for_resource_alignment(tmp_path):
    # started_at/ended_at são o que permite juntar resources.csv (30 s) e
    # db_cpu_cores.csv (5 s) a degraus — sem eles, nenhum dos dois sabe qual
    # degrau estava ativo.
    path = tmp_path / "requests.ndjson"
    _write_ndjson(path, _step_events(1000, "fine_up", count=30, latency_ms=3, start_offset_s=0))

    step = build_ramp_report(path, cell_id="e2-scylla", tier="medium")["steps"][0]

    assert step["started_at"] is not None and step["ended_at"] is not None
    assert step["started_at"] <= step["ended_at"]


def test_empty_ramp_yields_no_steps_and_an_undetermined_verdict(tmp_path):
    path = tmp_path / "requests.ndjson"
    path.write_text("")

    report = build_ramp_report(path, cell_id="e2-scylla", tier="medium")

    assert report["steps"] == []
    assert report["recovery"]["verdict"] == "undetermined"
    assert report["recovery"]["reason"] is not None


def test_lines_from_other_scenarios_are_ignored(tmp_path):
    path = tmp_path / "requests.ndjson"
    events = _step_events(1000, "fine_up", count=30, latency_ms=3, start_offset_s=0)
    events.append(
        {
            "scenario": "measurement",
            "timestamp": BASE.isoformat(),
            "latency_ms": 999,
            "status": 200,
            "returned_count": 20,
            "request_id": "0-0",
        }
    )
    _write_ndjson(path, events)

    report = build_ramp_report(path, cell_id="e2-scylla", tier="medium")

    assert len(report["steps"]) == 1
    assert report["steps"][0]["request_count"] == 30


def test_recovery_verdict_travels_with_the_report(tmp_path):
    path = tmp_path / "requests.ndjson"
    _write_ndjson(
        path,
        _step_events(4000, "fine_up", count=241, latency_ms=3, start_offset_s=0)
        + _step_events(4000, "fine_down", count=241, latency_ms=3, start_offset_s=600),
    )

    report = build_ramp_report(path, cell_id="e4-valkey", tier="medium")

    assert report["recovery"]["verdict"] in {"recovered", "lost", "undetermined"}
    assert report["cell_id"] == "e4-valkey"
    assert report["tier"] == "medium"


def _result(rate, phase, *, ok):
    return RampStepResult(
        rate=rate,
        phase=phase,
        throughput_rps=float(rate if ok else rate * 0.5),
        latency_ms_p99=3.0 if ok else 4000.0,
        offered_ratio=1.0 if ok else 0.5,
        offered_load_ok=ok,
        violated_slo=not ok,
    )


def test_knee_inside_the_fine_phase_reports_the_fine_resolution():
    results = [
        _result(1000, "coarse_up", ok=True),
        _result(2000, "coarse_up", ok=True),
        _result(3000, "fine_up", ok=True),
        _result(3100, "fine_up", ok=True),
        _result(3200, "fine_up", ok=False),
    ]
    knee = knee_summary(results)

    assert knee["first_violation_rate"] == 3200
    assert knee["resolved_in_fine_phase"] is True
    assert knee["resolution_rps"] == 100
    assert knee["warning"] is None


def test_knee_falling_in_the_coarse_phase_is_flagged_loudly():
    # O cenário levantado pelo usuário: o ponto de estresse ocorre ANTES do
    # projetado, cai na fase grossa e é medido com resolução 10x pior — sem
    # nenhum erro visível, e com a fase fina inteira desperdiçada em
    # sobrecarga.
    results = [
        _result(1000, "coarse_up", ok=True),
        _result(2000, "coarse_up", ok=True),
        _result(3000, "coarse_up", ok=False),
        _result(6000, "fine_up", ok=False),
        _result(6400, "fine_up", ok=False),
    ]
    knee = knee_summary(results)

    assert knee["first_violation_rate"] == 3000
    assert knee["resolved_in_fine_phase"] is False
    assert knee["resolution_rps"] == 1000
    assert "GROSSA" in knee["warning"]
    # A mensagem precisa dizer com que --knee refazer, ou quem lê tem de
    # recalcular na mão.
    assert "--knee" in knee["warning"]


def test_ramp_that_never_violates_says_the_knee_is_above_the_top():
    results = [_result(1000, "coarse_up", ok=True), _result(3000, "fine_up", ok=True)]
    knee = knee_summary(results)

    assert knee["first_violation_rate"] is None
    assert "ACIMA" in knee["warning"]


def test_knee_summary_travels_in_the_report(tmp_path):
    path = tmp_path / "requests.ndjson"
    _write_ndjson(path, _step_events(1000, "fine_up", count=30, latency_ms=3, start_offset_s=0))

    report = build_ramp_report(path, cell_id="e2-scylla", tier="medium")

    assert "knee" in report
    assert set(report["knee"]) == {
        "first_violation_rate",
        "first_violation_phase",
        "resolution_rps",
        "resolved_in_fine_phase",
        "warning",
    }


def test_step_results_on_an_empty_frame_is_empty(tmp_path):
    path = tmp_path / "requests.ndjson"
    path.write_text("")
    df = parse_requests_ndjson(path, scenarios=RAMP_SCENARIOS, extra_fields=RAMP_EXTRA_FIELDS)

    assert step_results(df) == []


def test_ignore_latency_flips_a_latency_only_violation(tmp_path):
    # rate=1, count=60 sobre 60s: throughput=1.0, offered_ratio~1.0 (>=0.95)
    # — isola a violação de p99, sem o portão de vazão ofertada interferir.
    path = tmp_path / "requests.ndjson"
    _write_ndjson(path, _step_events(1, "fine_up", count=60, latency_ms=250, start_offset_s=0))
    df = parse_requests_ndjson(path, scenarios=RAMP_SCENARIOS, extra_fields=RAMP_EXTRA_FIELDS)

    default = step_results(df)
    assert default[0].violated_slo is True

    ignoring = step_results(df, ignore_latency=True)
    assert ignoring[0].violated_slo is False


def test_ignore_latency_still_flags_a_real_error_rate_violation(tmp_path):
    path = tmp_path / "requests.ndjson"
    _write_ndjson(
        path,
        _step_events(1, "fine_up", count=59, latency_ms=3, start_offset_s=0)
        + _step_events(1, "fine_up", count=2, latency_ms=3, start_offset_s=59, status=500),
    )
    df = parse_requests_ndjson(path, scenarios=RAMP_SCENARIOS, extra_fields=RAMP_EXTRA_FIELDS)

    results = step_results(df, ignore_latency=True)
    assert results[0].violated_slo is True


def test_build_ramp_report_threads_ignore_latency_through(tmp_path):
    path = tmp_path / "requests.ndjson"
    _write_ndjson(path, _step_events(1, "fine_up", count=60, latency_ms=250, start_offset_s=0))

    default = build_ramp_report(path, cell_id="e4-valkey", tier="medium")
    assert default["steps"][0]["violated_slo"] is True

    ignoring = build_ramp_report(path, cell_id="e4-valkey", tier="medium", ignore_latency=True)
    assert ignoring["steps"][0]["violated_slo"] is False


def test_main_ignore_latency_slo_flag_flips_a_latency_only_violation(tmp_path, capsys):
    path = tmp_path / "requests.ndjson"
    _write_ndjson(path, _step_events(1, "fine_up", count=60, latency_ms=250, start_offset_s=0))
    out = tmp_path / "ramp_medium.json"

    main([str(path), "--cell", "e4-valkey", "--tier", "medium", "--out", str(out)])
    assert json.loads(out.read_text())["steps"][0]["violated_slo"] is True

    main(
        [
            str(path),
            "--cell",
            "e4-valkey",
            "--tier",
            "medium",
            "--out",
            str(out),
            "--ignore-latency-slo",
        ]
    )
    assert json.loads(out.read_text())["steps"][0]["violated_slo"] is False
