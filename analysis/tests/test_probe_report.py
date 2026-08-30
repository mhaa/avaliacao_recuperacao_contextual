"""Testa analysis/probe_report.py:violated_slo — CONTEXTO.md, "Protocolo de
medição": "SLO: p99 > 200 ms ou taxa de erro > 1%."."""

from __future__ import annotations

from analysis.probe_report import violated_slo


def test_violated_slo_false_when_within_both_thresholds():
    assert violated_slo({"latency_ms_p99": 150.0, "error_rate": 0.005}) is False


def test_violated_slo_true_when_latency_exceeds_threshold():
    assert violated_slo({"latency_ms_p99": 250.0, "error_rate": 0.0}) is True


def test_violated_slo_true_when_error_rate_exceeds_threshold():
    assert violated_slo({"latency_ms_p99": 50.0, "error_rate": 0.02}) is True


def test_violated_slo_true_when_no_requests_were_parsed():
    assert violated_slo({"latency_ms_p99": None, "error_rate": None}) is True
