"""Testa parse_k6_ndjson/build_summary contra um NDJSON sintético no mesmo
formato observado numa execução real de load/scenarios.js (ver README.md,
Etapa 7) — não depende de k6 nem de rede."""

from __future__ import annotations

import json

import polars as pl

from analysis.collect import build_summary, parse_k6_ndjson


def _point(metric: str, time: str, value: float, tags: dict) -> str:
    return json.dumps(
        {"metric": metric, "type": "Point", "data": {"time": time, "value": value, "tags": tags}}
    )


def _write_ndjson(path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n")


def test_parse_k6_ndjson_keeps_only_measurement_scenario_points(tmp_path):
    path = tmp_path / "k6-raw.json"
    lines = [
        _point(
            "http_req_duration",
            "2026-01-01T00:00:00.000Z",
            999.0,
            {"scenario": "warmup", "status": "200", "request_id": "0-0"},
        ),
        _point(
            "http_req_duration",
            "2026-01-01T00:02:00.000Z",
            12.5,
            {"scenario": "measurement", "status": "200", "request_id": "1-0"},
        ),
        _point(
            "returned_count",
            "2026-01-01T00:02:00.000Z",
            20,
            {"scenario": "measurement", "request_id": "1-0"},
        ),
        _point(
            "http_req_duration",
            "2026-01-01T00:02:01.000Z",
            15.0,
            {"scenario": "measurement", "status": "500", "request_id": "1-1"},
        ),
        _point(
            "returned_count",
            "2026-01-01T00:02:01.000Z",
            3,
            {"scenario": "measurement", "request_id": "1-1"},
        ),
    ]
    _write_ndjson(path, lines)

    df = parse_k6_ndjson(path)

    assert df.height == 2
    assert set(df["status"].to_list()) == {200, 500}
    row = df.filter(pl.col("status") == 200).row(0, named=True)
    assert row["latency_ms"] == 12.5
    assert row["returned_count"] == 20


def test_parse_k6_ndjson_ignores_non_point_events(tmp_path):
    path = tmp_path / "k6-raw.json"
    lines = [
        json.dumps({"type": "Metric", "metric": "http_req_duration", "data": {}}),
        _point(
            "http_req_duration",
            "2026-01-01T00:02:00.000Z",
            10.0,
            {"scenario": "measurement", "status": "200", "request_id": "1-0"},
        ),
    ]
    _write_ndjson(path, lines)

    df = parse_k6_ndjson(path)
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
