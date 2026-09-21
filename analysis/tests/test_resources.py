"""Testa só a lógica pura (parsing/escrita) de analysis/resources.py — a
coleta real via `docker stats` exige um container no ar, fora de escopo
para a camada "not integration" (mesmo padrão do resto do repositório)."""

from __future__ import annotations

import csv

import pytest

from analysis.resources import (
    ResourceSample,
    _cpu_delta_by_state,
    _cpu_utilization_from_deltas,
    _parse_mem_usage,
    _parse_percent,
    classify_bottleneck,
    write_resources_csv,
)


def test_parse_percent_strips_the_percent_sign():
    assert _parse_percent("12.34%") == 12.34


def test_parse_mem_usage_reads_the_used_side_in_mebibytes():
    assert _parse_mem_usage("512MiB / 2GiB") == 512.0


def test_parse_mem_usage_converts_gib_to_mib():
    assert _parse_mem_usage("1.5GiB / 4GiB") == pytest.approx(1536.0)


def test_parse_mem_usage_converts_kib_to_mib():
    assert _parse_mem_usage("2048KiB / 1GiB") == pytest.approx(2.0)


def test_write_resources_csv_round_trips(tmp_path):
    samples = [
        ResourceSample(component="database", cpu_percent=12.3, memory_mb=512.0),
        ResourceSample(component="service", cpu_percent=45.6, memory_mb=256.0),
    ]
    out = tmp_path / "resources.csv"
    write_resources_csv(samples, out)

    with out.open() as f:
        rows = list(csv.DictReader(f))

    assert rows[0]["component"] == "database"
    assert float(rows[0]["cpu_percent"]) == 12.3
    assert rows[1]["component"] == "service"
    assert rows[0]["memory_available_mb"] == ""


def test_write_resources_csv_includes_memory_available_when_present(tmp_path):
    samples = [
        ResourceSample(
            component="database", cpu_percent=12.3, memory_mb=512.0, memory_available_mb=2048.0
        ),
    ]
    out = tmp_path / "resources.csv"
    write_resources_csv(samples, out)

    with out.open() as f:
        rows = list(csv.DictReader(f))

    assert float(rows[0]["memory_available_mb"]) == 2048.0


def test_classify_bottleneck_ignores_memory_available_field():
    samples = [
        ResourceSample(
            component="database", cpu_percent=95.0, memory_mb=1000.0, memory_available_mb=None
        ),
        ResourceSample(component="service", cpu_percent=40.0, memory_mb=500.0),
    ]
    assert classify_bottleneck(samples) == "database_cpu"


def test_classify_bottleneck_picks_the_resource_closest_to_its_ceiling():
    samples = [
        ResourceSample(component="database", cpu_percent=95.0, memory_mb=1000.0),
        ResourceSample(component="service", cpu_percent=40.0, memory_mb=500.0),
        ResourceSample(component="loadgen", cpu_percent=30.0, memory_mb=500.0),
    ]
    assert classify_bottleneck(samples) == "database_cpu"


def test_classify_bottleneck_considers_memory_when_a_ceiling_is_given():
    samples = [
        ResourceSample(component="database", cpu_percent=50.0, memory_mb=7000.0),
        ResourceSample(component="service", cpu_percent=40.0, memory_mb=500.0),
    ]
    result = classify_bottleneck(samples, memory_ceiling_mb={"database": 8000.0, "service": 16000.0})
    assert result == "database_memory"


def test_classify_bottleneck_considers_network_when_present():
    samples = [
        ResourceSample(component="loadgen", cpu_percent=20.0, memory_mb=500.0, network_mbps=850.0),
        ResourceSample(component="database", cpu_percent=20.0, memory_mb=500.0),
    ]
    assert classify_bottleneck(samples) == "loadgen_network"


def test_classify_bottleneck_raises_without_samples():
    with pytest.raises(ValueError):
        classify_bottleneck([])


def test_cpu_delta_by_state_sums_deltas_across_cores_sharing_a_state():
    raw_series = [
        ("idle", [100.0, 130.0]),  # cpu0, delta 30
        ("idle", [50.0, 65.0]),  # cpu1, delta 15
        ("user", [10.0, 40.0]),  # cpu0, delta 30
        ("user", [5.0, 10.0]),  # cpu1, delta 5
    ]
    assert _cpu_delta_by_state(raw_series) == {"idle": 45.0, "user": 35.0}


def test_cpu_delta_by_state_ignores_series_with_fewer_than_2_points():
    raw_series = [("idle", [100.0]), ("user", [10.0, 20.0])]
    assert _cpu_delta_by_state(raw_series) == {"user": 10.0}


def test_cpu_utilization_from_deltas_is_1_minus_idle_fraction():
    # 45s ociosos + 35s ocupados = 80s totais -> 35/80 = 43.75% de uso
    result = _cpu_utilization_from_deltas({"idle": 45.0, "user": 35.0})
    assert result == pytest.approx(43.75)


def test_cpu_utilization_from_deltas_treats_missing_idle_as_fully_busy():
    result = _cpu_utilization_from_deltas({"user": 10.0, "system": 5.0})
    assert result == pytest.approx(100.0)


def test_cpu_utilization_from_deltas_raises_when_total_delta_is_zero():
    with pytest.raises(ValueError):
        _cpu_utilization_from_deltas({})
