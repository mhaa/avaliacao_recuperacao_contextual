"""Testa parse_requests_ndjson/build_summary contra um NDJSON sintético no
mesmo formato que load/scenarios.js escreve via console.log() por
requisição (ver README.md, Etapa 7) — não depende de k6 nem de rede."""

from __future__ import annotations

import json

import polars as pl

from analysis.collect import build_summary, parse_requests_ndjson


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
