"""Testa só a lógica pura (parsing/escrita) de analysis/resources.py — a
coleta real via `docker stats` exige um container no ar, fora de escopo
para a camada "not integration" (mesmo padrão do resto do repositório)."""

from __future__ import annotations

import csv

import pytest

from analysis.resources import ResourceSample, _parse_mem_usage, _parse_percent, write_resources_csv


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
