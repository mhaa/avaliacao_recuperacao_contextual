"""resources.csv — CPU, memória e rede do banco, do serviço e do gerador
(CONTEXTO.md, "Métricas" / atribuição de gargalo). Duas fontes atrás de uma
interface comum (`ResourceCollector`), mesmo padrão de storage/base.py (uma
interface, vários backends): `docker stats` localmente (um host Docker só,
nunca para números que entram no TCC) vs. API do GCP Cloud Monitoring na
nuvem (VMs separadas, infra/modules/{database,service,loadgen} — três VMs,
IMPLEMENTACAO.md).
"""

from __future__ import annotations

import csv
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ResourceSample:
    component: str
    cpu_percent: float
    memory_mb: float
    # Rede e timestamp só existem de verdade na coleta em nuvem (amostragem
    # a cada 5s durante a rampa de confirmação) — None no `docker stats`
    # local, que é um snapshot instantâneo sem série temporal.
    network_mbps: float | None = None
    timestamp: datetime | None = None


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
    """Nuvem — API do Cloud Monitoring (`google-cloud-monitoring`).

    CPU e rede vêm das métricas padrão do Compute Engine, sem agente
    nenhum (`compute.googleapis.com/instance/{cpu/utilization,
    network/{received,sent}_bytes_count}`). Memória exige um coletor nas 3
    VMs (infra/modules/{database,service,loadgen}/main.tf, startup-script)
    — COS não tem gerenciador de pacotes, então o Ops Agent oficial do
    Google (pensado para instalação via apt/yum) não se aplica; usamos o
    OpenTelemetry Collector Contrib como contêiner (`hostmetrics` receiver
    + exporter `googlecloud`), que exporta sob o prefixo
    `workload.googleapis.com/` — ver README.md, "Instrumentação de
    gargalo". Sem o coletor rodando, a métrica não tem série temporal
    nenhuma para a janela pedida, e `collect()` deixa o `ValueError` da
    API subir em vez de inventar um valor.

    `memory_mb_by_component` converte a fração (0-1) que o `hostmetrics`
    reporta para MB usando a memória nominal do tipo de máquina de cada VM
    (já conhecida em infra/modules/*/main.tf, sem precisar de uma segunda
    chamada à API do Compute para descobrir o tipo de máquina em tempo de
    execução).
    """

    _CPU_METRIC = "compute.googleapis.com/instance/cpu/utilization"
    _NETWORK_RECEIVED_METRIC = "compute.googleapis.com/instance/network/received_bytes_count"
    _NETWORK_SENT_METRIC = "compute.googleapis.com/instance/network/sent_bytes_count"
    # OTel `system.memory.utilization` é uma fração (0-1) por estado
    # (used/free/buffered/cached) — só "used" interessa aqui; nome/label
    # exatos ainda sem validação contra uma VM real (ver README.md,
    # "Riscos conhecidos").
    _MEMORY_METRIC = "workload.googleapis.com/system.memory.utilization"
    _MEMORY_STATE_FILTER = 'metric.labels.state = "used"'

    def __init__(
        self,
        project_id: str,
        instance_by_component: dict[str, str],
        memory_mb_by_component: dict[str, float],
        start_time: datetime,
        end_time: datetime,
    ):
        self._project_id = project_id
        self._instance_by_component = instance_by_component
        self._memory_mb_by_component = memory_mb_by_component
        self._start_time = start_time
        self._end_time = end_time

    def collect(self) -> list[ResourceSample]:
        # Import tardio: só precisa de `google-cloud-monitoring`/rede real
        # quando efetivamente coletando na nuvem — não em cada `import
        # analysis.resources` local/em teste.
        from google.cloud import monitoring_v3

        client = monitoring_v3.MetricServiceClient()
        project_name = f"projects/{self._project_id}"
        interval = monitoring_v3.TimeInterval(
            start_time=self._start_time, end_time=self._end_time
        )
        window_seconds = max((self._end_time - self._start_time).total_seconds(), 1.0)

        samples: list[ResourceSample] = []
        for component, instance_name in self._instance_by_component.items():
            cpu_fraction = self._mean_value(
                client, project_name, self._CPU_METRIC, instance_name, interval
            )
            received_bytes = self._mean_value(
                client, project_name, self._NETWORK_RECEIVED_METRIC, instance_name, interval
            )
            sent_bytes = self._mean_value(
                client, project_name, self._NETWORK_SENT_METRIC, instance_name, interval
            )
            memory_fraction = self._mean_value(
                client,
                project_name,
                self._MEMORY_METRIC,
                instance_name,
                interval,
                extra_filter=self._MEMORY_STATE_FILTER,
            )
            memory_capacity_mb = self._memory_mb_by_component[component]
            samples.append(
                ResourceSample(
                    component=component,
                    cpu_percent=cpu_fraction * 100.0,
                    memory_mb=memory_fraction * memory_capacity_mb,
                    network_mbps=(received_bytes + sent_bytes) * 8 / 1_000_000 / window_seconds,
                    timestamp=self._end_time,
                )
            )
        return samples

    def _mean_value(
        self, client, project_name, metric_type, instance_name, interval, extra_filter: str = ""
    ) -> float:
        filter_str = (
            f'metric.type = "{metric_type}" AND resource.labels.instance_id = "{instance_name}"'
        )
        if extra_filter:
            filter_str += f" AND {extra_filter}"
        results = client.list_time_series(
            request={
                "name": project_name,
                "filter": filter_str,
                "interval": interval,
                "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            }
        )
        points = [p.value.double_value for series in results for p in series.points]
        if not points:
            raise ValueError(
                f"nenhuma série temporal para {metric_type!r} em {instance_name!r} na janela "
                f"pedida — para a métrica de memória, confirme que o Ops Agent está rodando "
                f"nessa VM (infra/modules/*/main.tf)."
            )
        return sum(points) / len(points)


# Tetos usados por classify_bottleneck() para decidir "perto/além do
# próprio teto" — não são o SLO da aplicação (p99>200ms), são limites de
# saturação do recurso em si.
DEFAULT_CPU_CEILING_PERCENT = 90.0
DEFAULT_NETWORK_CEILING_MBPS = 900.0  # ~90% de uma NIC de 1 Gbps


def classify_bottleneck(
    samples: list[ResourceSample],
    cpu_ceiling: float = DEFAULT_CPU_CEILING_PERCENT,
    memory_ceiling_mb: dict[str, float] | None = None,
    network_ceiling_mbps: float = DEFAULT_NETWORK_CEILING_MBPS,
) -> str:
    """Dado um conjunto de amostras (tipicamente a última rodada de
    sondagem antes de violar o SLO — SaturationSearchResult.probes, ver
    load/saturation.py), retorna qual `<component>_<resource>` está mais
    perto/além do próprio teto (ex.: "database_cpu", "service_memory",
    "loadgen_network") — a razão amostra/teto mais alta vence. Precisa de
    pelo menos uma amostra com CPU preenchida; memória/rede ausentes
    (`None`) são ignoradas nessa comparação, não tratadas como zero."""
    memory_ceiling_mb = memory_ceiling_mb or {}
    best_component_resource = None
    best_ratio = -1.0

    for sample in samples:
        candidates = [("cpu", sample.cpu_percent / cpu_ceiling)]
        ceiling_mb = memory_ceiling_mb.get(sample.component)
        if ceiling_mb:
            candidates.append(("memory", sample.memory_mb / ceiling_mb))
        if sample.network_mbps is not None:
            candidates.append(("network", sample.network_mbps / network_ceiling_mbps))

        for resource, ratio in candidates:
            if ratio > best_ratio:
                best_ratio = ratio
                best_component_resource = f"{sample.component}_{resource}"

    if best_component_resource is None:
        raise ValueError("nenhuma amostra para classificar o gargalo")
    return best_component_resource


def write_resources_csv(samples: list[ResourceSample], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["component", "timestamp", "cpu_percent", "memory_mb", "network_mbps"])
        for sample in samples:
            writer.writerow(
                [
                    sample.component,
                    sample.timestamp.isoformat() if sample.timestamp else "",
                    sample.cpu_percent,
                    sample.memory_mb,
                    sample.network_mbps if sample.network_mbps is not None else "",
                ]
            )
