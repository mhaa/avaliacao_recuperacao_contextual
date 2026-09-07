"""Testa parse_requests_ndjson/build_summary contra um NDJSON sintético no
mesmo formato que load/scenarios.js escreve via console.log() por
requisição (ver README.md, Etapa 7) — não depende de k6 nem de rede."""

from __future__ import annotations

import json

import polars as pl

from analysis.collect import (
    MIN_OFFERED_RATIO,
    build_summary,
    collect,
    offered_load_fields,
    parse_requests_ndjson,
)


def _request(
    scenario: str, timestamp: str, latency_ms: float, status: int, returned_count: int | None
) -> str:
    return json.dumps(
        {
            "request_id": "0-0",
            "scenario": scenario,
            "timestamp": timestamp,
            "latency_ms": latency_ms,
            "status": status,
            "returned_count": returned_count,
        }
    )


def _write_ndjson(path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")


def test_parse_requests_ndjson_keeps_only_measurement_scenario_lines(tmp_path):
    path = tmp_path / "requests.ndjson"
    lines = [
        _request("warmup", "2026-01-01T00:00:00.000Z", 999.0, 200, 20),
        _request("measurement", "2026-01-01T00:02:00.000Z", 12.5, 200, 20),
        _request("measurement", "2026-01-01T00:02:01.000Z", 15.0, 500, 3),
    ]
    _write_ndjson(path, lines)

    df = parse_requests_ndjson(path)

    assert df.height == 2
    assert set(df["status"].to_list()) == {200, 500}
    row = df.filter(pl.col("status") == 200).row(0, named=True)
    assert row["latency_ms"] == 12.5
    assert row["returned_count"] == 20


def test_parse_requests_ndjson_ignores_blank_lines(tmp_path):
    path = tmp_path / "requests.ndjson"
    lines = [
        "",
        _request("measurement", "2026-01-01T00:02:00.000Z", 10.0, 200, 20),
    ]
    _write_ndjson(path, lines)

    df = parse_requests_ndjson(path)
    assert df.height == 1


def test_build_summary_never_reports_a_mean_only_percentiles():
    df = pl.DataFrame(
        {
            "timestamp": pl.datetime_range(
                start=pl.datetime(2026, 1, 1, 0, 0, 0),
                end=pl.datetime(2026, 1, 1, 0, 0, 9),
                interval="1s",
                eager=True,
            ),
            "latency_ms": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 100.0],
            "status": [200] * 9 + [500],
            "returned_count": [20] * 10,
        }
    )
    summary = build_summary(df)

    assert "latency_ms_mean" not in summary
    assert summary["request_count"] == 10
    assert summary["error_rate"] == 0.1
    assert summary["latency_ms_p50"] is not None
    assert summary["throughput_rps"] == 10 / 9


def test_offered_load_fields_flags_a_shortfall_below_the_threshold():
    # docs/DESIGN.md, "Vazão ofertada verificada, não presumida" — e1-valkey
    # na triagem entregou ~43% do alvo (k6 descartou chegadas por maxVUs).
    summary = {"throughput_rps": 430.0}
    fields = offered_load_fields(summary, target_rate=1000)
    assert fields["offered_ratio"] == 0.43
    assert fields["offered_load_ok"] is False


def test_offered_load_fields_accepts_scheduling_jitter_within_the_threshold():
    summary = {"throughput_rps": 990.0}
    fields = offered_load_fields(summary, target_rate=1000)
    assert fields["offered_ratio"] == 0.99
    assert fields["offered_ratio"] >= MIN_OFFERED_RATIO
    assert fields["offered_load_ok"] is True


def test_offered_load_fields_is_all_none_without_a_target_or_throughput():
    assert offered_load_fields({"throughput_rps": 100.0}, target_rate=None) == {
        "target_rate": None,
        "offered_ratio": None,
        "offered_load_ok": None,
    }
    assert offered_load_fields({"throughput_rps": None}, target_rate=1000)[
        "offered_load_ok"
    ] is None


def test_collect_reads_the_target_rate_from_the_manifest(tmp_path):
    # 2 requisições em 10s de span = 0,2 req/s contra alvo de 100 —
    # offered_load_ok=False precisa chegar ao summary.json gravado.
    lines = [
        _request("measurement", "2026-01-01T00:02:00.000Z", 10.0, 200, 20),
        _request("measurement", "2026-01-01T00:02:10.000Z", 12.0, 200, 20),
    ]
    _write_ndjson(tmp_path / "requests.ndjson", lines)
    (tmp_path / "manifest.json").write_text(json.dumps({"rate": 100, "smoke": False}))

    collect(tmp_path)

    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["target_rate"] == 100
    assert summary["offered_load_ok"] is False


def test_collect_without_manifest_leaves_offered_load_fields_none(tmp_path):
    # Diretórios de sondagem de saturação não têm manifest.json — o portão
    # deles vive em analysis/probe_report.py, nunca inventado aqui.
    lines = [_request("measurement", "2026-01-01T00:02:00.000Z", 10.0, 200, 20)]
    _write_ndjson(tmp_path / "requests.ndjson", lines)

    collect(tmp_path)

    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["target_rate"] is None
    assert summary["offered_ratio"] is None
    assert summary["offered_load_ok"] is None


def test_build_summary_handles_empty_dataframe():
    empty = pl.DataFrame(
        schema={
            "timestamp": pl.Datetime,
            "latency_ms": pl.Float64,
            "status": pl.Int32,
            "returned_count": pl.Int32,
        }
    )
    summary = build_summary(empty)
    assert summary["request_count"] == 0
    assert summary["latency_ms_p50"] is None
