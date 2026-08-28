"""resources.csv — CPU e memória do banco, do serviço e do gerador
(CONTEXTO.md, "Métricas"). Duas fontes atrás de uma interface comum
(`ResourceCollector`), mesmo padrão de storage/base.py (uma interface,
vários backends): `docker stats` localmente (um host Docker só) vs. API do
GCP Cloud Monitoring na nuvem (VMs separadas, sem daemon Docker
compartilhado — infra/ da Etapa 9 ainda não existe).
"""

from __future__ import annotations

import csv
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ResourceSample:
    component: str
    cpu_percent: float
    memory_mb: float


class ResourceCollector(Protocol):
    def collect(self) -> list[ResourceSample]: ...


def _parse_percent(value: str) -> float:
    return float(value.rstrip("%"))


def _to_mb(value: str) -> float:
    """`docker stats` reporta memória com sufixo de unidade (ex.: "123.4MiB",
    "1.2GiB") — só o valor USADO importa aqui."""
    units = {"GiB": 1024.0, "MiB": 1.0, "KiB": 1.0 / 1024.0, "B": 1.0 / (1024.0 * 1024.0)}
    for unit, factor in units.items():
        if value.endswith(unit):
            return float(value[: -len(unit)]) * factor
    raise ValueError(f"unidade de memória desconhecida em {value!r}")


def _parse_mem_usage(value: str) -> float:
    """"MemUsage" do `docker stats` vem como "123.4MiB / 2GiB"."""
    used = value.split("/")[0].strip()
    return _to_mb(used)


class DockerStatsCollector:
    """Local apenas — CONTEXTO.md proíbe medir aqui; usado só para validar
    o formato de resources.csv, nunca para números que entram no TCC."""

    def __init__(self, container_by_component: dict[str, str]):
        self._container_by_component = container_by_component

    def collect(self) -> list[ResourceSample]:
        samples = []
        for component, container_name in self._container_by_component.items():
            result = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{json .}}", container_name],
                capture_output=True,
                text=True,
                check=True,
            )
            stats = json.loads(result.stdout)
            samples.append(
                ResourceSample(
                    component=component,
                    cpu_percent=_parse_percent(stats["CPUPerc"]),
                    memory_mb=_parse_mem_usage(stats["MemUsage"]),
                )
            )
        return samples


class GCPMonitoringCollector:
    """Nuvem — API do Cloud Monitoring. Levanta NotImplementedError de
    propósito: infra/ (Etapa 9) ainda não existe e o usuário ainda não tem
    projeto GCP, então fingir suporte aqui esconderia isso."""

    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "coleta de recursos na nuvem depende de infra/ (Etapa 9), ainda não implementada"
        )

    def collect(self) -> list[ResourceSample]:
        raise NotImplementedError


def write_resources_csv(samples: list[ResourceSample], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["component", "cpu_percent", "memory_mb"])
        for sample in samples:
            writer.writerow([sample.component, sample.cpu_percent, sample.memory_mb])
