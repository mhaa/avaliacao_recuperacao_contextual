"""resources.csv — CPU, memória e rede do banco, do serviço e do gerador
(docs/DESIGN.md, "Métricas" / atribuição de gargalo). Duas fontes atrás de uma
interface comum (`ResourceCollector`), mesmo padrão de storage/base.py (uma
interface, vários backends): `docker stats` localmente (um host Docker só,
nunca para números que entram no TCC) vs. API do GCP Cloud Monitoring na
nuvem (VMs separadas, infra/modules/{database,service,loadgen} — três VMs,
docs/ARCHITECTURE.md).
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
    # MemAvailable (system.linux.memory.available) — métrica opcional do
    # scraper hostmetrics/memory, lida em modo best-effort por
    # GCPMonitoringCollector; None enquanto a série ainda não propagou ou
    # sempre no `docker stats` local, que não tem equivalente.
    memory_available_mb: float | None = None


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
    """Local apenas — docs/DESIGN.md proíbe medir aqui; usado só para validar
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

    Memória já vem em MB direto de `_MEMORY_METRIC` (bytes, convertidos
    abaixo) — nenhuma conversão de fração por tipo de máquina é necessária.

    `_MEMORY_AVAILABLE_METRIC` (MemAvailable do `/proc/meminfo`) é uma
    métrica separada e opcional do mesmo scraper `hostmetrics/memory` —
    diferente de `system.memory.usage`, não vem habilitada por padrão no
    OTel Collector Contrib 0.112.0 (precisou de
    `scrapers.memory.metrics."system.linux.memory.available".enabled:
    true` nos 3 `main.tf`). Por ser recém-habilitada e nada no projeto
    depender dela ainda, é lida em modo best-effort em `collect()`: se a
    série ainda não propagou, o campo fica `None` em vez de derrubar a
    coleta inteira (CPU/rede/memória-used continuam obrigatórias).
    """

    _CPU_METRIC = "compute.googleapis.com/instance/cpu/utilization"
    _NETWORK_RECEIVED_METRIC = "compute.googleapis.com/instance/network/received_bytes_count"
    _NETWORK_SENT_METRIC = "compute.googleapis.com/instance/network/sent_bytes_count"
    # `system.memory.utilization` (fração 0-1) NÃO existe no exporter
    # `googlecloud` do OTel Collector Contrib 0.112.0 — confirmado ao vivo
    # listando os metric descriptors reais de `workload.googleapis.com/*`
    # deste projeto: o hostmetrics receiver exporta `system.memory.usage`
    # (bytes em uso, por estado — used/free/buffered/cached/slab), não uma
    # utilização. Usar o nome errado gerava 404 "metric not found" (nunca
    # existiu, não era atraso de criação de descritor como parecia a
    # princípio). Valor é INT64 (bytes), não DOUBLE — ver `_mean_value`.
    _MEMORY_METRIC = "workload.googleapis.com/system.memory.usage"
    _MEMORY_STATE_FILTER = 'metric.labels.state = "used"'
    # MemAvailable — sem atributo `state` (diferente de system.memory.usage).
    _MEMORY_AVAILABLE_METRIC = "workload.googleapis.com/system.linux.memory.available"

    def __init__(
        self,
        project_id: str,
        instance_by_component: dict[str, str],
        start_time: datetime,
        end_time: datetime,
    ):
        """`instance_by_component` precisa do ID NUMÉRICO de cada VM (ex.:
        `google_compute_instance.<x>.instance_id` no Terraform,
        `outputs["database_instance_id"]` em run_measurement_battery.py), não
        o nome (`tcc-<cell>-database`) — `resource.labels.instance_id` do
        Cloud Monitoring para métricas `gce_instance` usa o ID numérico.
        Passar o nome faz `_mean_value` nunca casar nenhuma série temporal
        (confirmado ao vivo — parecia atraso de propagação, não era)."""
        self._project_id = project_id
        self._instance_by_component = instance_by_component
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
            memory_bytes = self._mean_value(
                client,
                project_name,
                self._MEMORY_METRIC,
                instance_name,
                interval,
                extra_filter=self._MEMORY_STATE_FILTER,
            )
            # Best-effort: MemAvailable é recém-habilitada (ver docstring da
            # classe) — uma janela sem série ainda não deve derrubar CPU/
            # rede/memória-used, que já funcionam e nada aqui depende dela.
            try:
                memory_available_bytes = self._mean_value(
                    client, project_name, self._MEMORY_AVAILABLE_METRIC, instance_name, interval
                )
                memory_available_mb = memory_available_bytes / (1024**2)
            except ValueError:
                memory_available_mb = None
            samples.append(
                ResourceSample(
                    component=component,
                    cpu_percent=cpu_fraction * 100.0,
                    memory_mb=memory_bytes / (1024**2),
                    network_mbps=(received_bytes + sent_bytes) * 8 / 1_000_000 / window_seconds,
                    timestamp=self._end_time,
                    memory_available_mb=memory_available_mb,
                )
            )
        return samples

    @staticmethod
    def _point_value(value) -> float:
        """`TypedValue` é um `oneof` — métricas de utilização (CPU) vêm como
        DOUBLE, mas contagens/bytes (rede, `system.memory.usage`) vêm como
        INT64. Ler sempre `.double_value` devolve 0.0 silenciosamente para
        as INT64 (campo não populado do oneof, default de proto3) — bug real
        encontrado ao vivo: a memória sempre dava 0 sem erro nenhum, só
        depois de comparar contra o descritor real (`value_type: INT64`) da
        métrica é que apareceu."""
        if "int64_value" in value:
            return float(value.int64_value)
        return value.double_value

    def _mean_value(
        self, client, project_name, metric_type, instance_name, interval, extra_filter: str = ""
    ) -> float:
        # Mesmo motivo do import tardio em collect(): _mean_value é um método
        # próprio, não herda o `monitoring_v3` importado localmente lá — sem
        # isso, toda chamada real batia em `NameError: name 'monitoring_v3' is
        # not defined` (nunca pego pelos testes, que usam um client fake sem
        # passar por aqui de verdade; só uma invocação real contra o Cloud
        # Monitoring expôs isso).
        from google.cloud import monitoring_v3

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
        points = [self._point_value(p.value) for series in results for p in series.points]
        if not points:
            hint = ""
            if metric_type.startswith("workload.googleapis.com/"):
                hint = self._diagnose_generic_node_collision(client, project_name, metric_type, interval)
            raise ValueError(
                f"nenhuma série temporal para {metric_type!r} em {instance_name!r} na janela "
                f"pedida — para a métrica de memória, confirme que o Ops Agent está rodando "
                f"nessa VM (infra/modules/*/main.tf).{hint}"
            )
        return sum(points) / len(points)

    @staticmethod
    def _diagnose_generic_node_collision(client, project_name, metric_type, interval) -> str:
        """Consulta extra, só quando a principal não achou nada — filtra por
        `resource.type = "generic_node"` (sem `instance_id`: esse tipo de
        recurso nem tem esse label, por isso a consulta principal nunca casa
        nada quando esse bug ocorre, em vez de reportar "recurso errado").
        Detecta rapidamente o bug real encontrado ao vivo: o coletor OTel sem
        o processor `resourcedetection` faz TODAS as VMs caírem no mesmo
        recurso "genérico" em branco, causando colisões de escrita entre elas
        (ver infra/modules/database/main.tf). Best-effort: qualquer erro
        nesta consulta de diagnóstico é engolido — nunca deve mascarar o
        ValueError original por uma falha na checagem extra."""
        from google.cloud import monitoring_v3

        try:
            results = client.list_time_series(
                request={
                    "name": project_name,
                    "filter": f'metric.type = "{metric_type}" AND resource.type = "generic_node"',
                    "interval": interval,
                    "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.HEADERS,
                }
            )
            if any(True for _ in results):
                return (
                    " DIAGNÓSTICO: existem séries dessa métrica sob resource.type="
                    "'generic_node' (não 'gce_instance') — o coletor OTel está sem o "
                    "processor `resourcedetection` (detectors: [gcp]) no pipeline, ou "
                    "ele não conseguiu falar com o metadata server. Isso faz várias VMs "
                    "colidirem na mesma identidade de recurso em branco no Cloud "
                    "Monitoring (ver infra/modules/database/main.tf)."
                )
        except Exception:
            pass
        return ""


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
        writer.writerow(
            [
                "component",
                "timestamp",
                "cpu_percent",
                "memory_mb",
                "network_mbps",
                "memory_available_mb",
            ]
        )
        for sample in samples:
            writer.writerow(
                [
                    sample.component,
                    sample.timestamp.isoformat() if sample.timestamp else "",
                    sample.cpu_percent,
                    sample.memory_mb,
                    sample.network_mbps if sample.network_mbps is not None else "",
                    sample.memory_available_mb if sample.memory_available_mb is not None else "",
                ]
            )
