"""Recursos da campanha de rampa: CPU do banco POR NÚCLEO, e o alinhamento
entre séries temporais e degraus (docs/DESIGN.md, "Experimento complementar").

Módulo separado de `analysis/resources.py`, de propósito, por duas razões:

1. `write_resources_csv` e o `ResourceSample` da bateria principal ficam
   intocados — formato de saída já gravado em disco não muda por causa de um
   experimento complementar.
2. A cadência é outra. `resources.csv` amostra a cada 30 s via Cloud
   Monitoring; isto amostra a cada 5 s lendo `/proc/stat` na própria VM do
   banco, e produz uma linha POR NÚCLEO. Enfiar as duas coisas na mesma
   tabela produziria linhas com semântica diferente sob o mesmo cabeçalho.

Por que ler /proc/stat em vez de usar o que já existe:

- **Valkey.** `analysis/resources.py:classify_bottleneck` deriva CPU de
  `1 - idle/total` SOMADO sobre todos os núcleos. O caminho de dados do
  Valkey é de thread única: saturado, ele lê ~12,5% numa VM de 8 vCPU, e o
  veredito automático jamais acusaria `database_cpu` nas células Valkey. O
  máximo por núcleo é o que torna essa saturação visível.
- **Resolução.** O coletor OTel usa `collection_interval: 60s` e o Cloud
  Monitoring recusa mais de 1 ponto/min para `workload.googleapis.com/*`,
  com janela de consulta de 150 s — um degrau de 1 min renderia um ponto
  borrado sobre ~2,5 degraus.

É o mesmo mecanismo, e a mesma aritmética, que o projeto já escolheu para a
CPU do gerador (`analysis/probe_report.py:_cpu_percent_from_stat`), pelo
segundo motivo acima.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Separador de snapshot no log que o amostrador grava na VM do banco. Linha
# própria, e não um prefixo por linha, para o log continuar sendo /proc/stat
# literal — assim o parser aqui e um `grep` manual leem a mesma coisa.
SNAPSHOT_MARKER = "==="


@dataclass(frozen=True)
class ProcStatSnapshot:
    timestamp: datetime
    # "cpu" (agregado) e "cpu0", "cpu1", ... (por núcleo) → jiffies
    # acumulados desde o boot.
    per_core: dict[str, tuple[int, ...]]


@dataclass(frozen=True)
class DbCpuCoreSample:
    timestamp: datetime
    core: str
    cpu_percent: float


def parse_proc_stat_log(text: str) -> list[ProcStatSnapshot]:
    """Log de snapshots de /proc/stat separados por linhas `=== <iso8601>`.

    Linhas que não começam com `cpu` são ignoradas: /proc/stat traz muito
    mais (intr, ctxt, btime, processes...), e o amostrador grava o arquivo
    inteiro em vez de filtrar na origem — filtrar na VM exigiria lógica na
    linha de comando remota, que é onde erro fica invisível.
    """
    snapshots: list[ProcStatSnapshot] = []
    timestamp: datetime | None = None
    per_core: dict[str, tuple[int, ...]] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(SNAPSHOT_MARKER):
            if timestamp is not None and per_core:
                snapshots.append(ProcStatSnapshot(timestamp, per_core))
            timestamp = datetime.fromisoformat(line[len(SNAPSHOT_MARKER) :].strip())
            per_core = {}
            continue
        if not line.startswith("cpu"):
            continue
        fields = line.split()
        # 8 campos: user nice system idle iowait irq softirq steal. Kernels
        # novos acrescentam guest/guest_nice no fim; truncar mantém a conta
        # idêntica à de probe_report.py.
        per_core[fields[0]] = tuple(int(x) for x in fields[1:9])

    if timestamp is not None and per_core:
        snapshots.append(ProcStatSnapshot(timestamp, per_core))
    return snapshots


def _cpu_percent(before: tuple[int, ...], after: tuple[int, ...]) -> float:
    """Mesma aritmética de `analysis/probe_report.py:_cpu_percent_from_stat`:
    idle = idle + iowait, percentual = 1 - (delta idle / delta total).

    Com uma defesa a mais: o resultado é preso em [0, 100]. Os contadores de
    /proc/stat são acumulados desde o boot e nunca deveriam retroceder, mas
    migração de VM ao vivo e reinício de contador acontecem — e um delta de
    ocioso negativo produziria um "114% de CPU" que entraria calado na
    figura do TCC. Preso, um episódio desses vira 100% (implausível de
    notar) em vez de um número impossível.
    """
    idle_before, idle_after = before[3] + before[4], after[3] + after[4]
    delta_total = sum(after) - sum(before)
    if delta_total <= 0:
        return 0.0
    percent = 100.0 * (delta_total - (idle_after - idle_before)) / delta_total
    return min(100.0, max(0.0, percent))


def samples_from_log(text: str) -> list[DbCpuCoreSample]:
    """Um par consecutivo de snapshots vira uma amostra por núcleo.

    Carimbada com o timestamp do snapshot FINAL do par: é o instante em que a
    janela de medição se fecha, e é o que faz o alinhamento por intervalo
    contra `started_at`/`ended_at` dos degraus ficar correto — carimbar com o
    inicial atribuiria ao degrau anterior uma janela que já pertence ao
    seguinte.
    """
    snapshots = parse_proc_stat_log(text)
    samples: list[DbCpuCoreSample] = []
    for before, after in zip(snapshots, snapshots[1:]):
        for core, after_fields in after.per_core.items():
            before_fields = before.per_core.get(core)
            if before_fields is None:
                continue
            samples.append(
                DbCpuCoreSample(
                    timestamp=after.timestamp,
                    core=core,
                    cpu_percent=_cpu_percent(before_fields, after_fields),
                )
            )
    return samples


def write_db_cpu_cores_csv(samples: list[DbCpuCoreSample], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "core", "cpu_percent"])
        for s in samples:
            writer.writerow([s.timestamp.isoformat(), s.core, f"{s.cpu_percent:.2f}"])


def max_core_percent(samples: list[DbCpuCoreSample]) -> float | None:
    """Maior utilização de um núcleo INDIVIDUAL, ignorando a linha agregada
    `cpu`. É este número, e não o agregado, que denuncia a saturação de um
    banco de thread única."""
    per_core = [s.cpu_percent for s in samples if s.core != "cpu"]
    return max(per_core) if per_core else None


def step_windows(steps: list[dict]) -> list[tuple[datetime, datetime, tuple[int, str]]]:
    """Janelas `(início, fim, (taxa, fase))` de cada degrau que tenha
    carimbos de tempo. Degrau sem requisição nenhuma não tem janela e some
    daqui — atribuir amostra a ele inventaria dado."""
    windows = []
    for step in steps:
        if not step.get("started_at") or not step.get("ended_at"):
            continue
        windows.append(
            (
                datetime.fromisoformat(step["started_at"]),
                datetime.fromisoformat(step["ended_at"]),
                (int(step["rate"]), step["phase"]),
            )
        )
    return windows


def find_step(windows, timestamp: datetime) -> tuple[int, str] | None:
    """Primeiro degrau cuja janela contém `timestamp`; None se nenhum.

    "Primeiro que casa vence" importa: janelas vizinhas podem encostar no
    mesmo instante, e contar a amostra duas vezes inflaria a CPU média de
    ambos os degraus.
    """
    for start, end, key in windows:
        if start <= timestamp <= end:
            return key
    return None


def align_samples_to_steps(
    samples: list[DbCpuCoreSample], steps: list[dict]
) -> dict[tuple[int, str], list[DbCpuCoreSample]]:
    """Join por intervalo: cada amostra é atribuída ao degrau cuja janela
    `[started_at, ended_at]` a contém.

    Necessário porque nem `resources.csv` nem `db_cpu_cores.csv` sabem qual
    degrau estava ativo — são séries temporais puras. A chave é (taxa, fase),
    e não só a taxa, porque a descida repete as taxas da subida.

    Amostras fora de qualquer janela são DESCARTADAS em silêncio: caem nos
    intervalos entre degraus (troca de estágio do k6) ou no setup/teardown, e
    atribuí-las ao vizinho mais próximo inventaria dado.
    """
    windows = []
    for step in steps:
        if not step.get("started_at") or not step.get("ended_at"):
            continue
        windows.append(
            (
                datetime.fromisoformat(step["started_at"]),
                datetime.fromisoformat(step["ended_at"]),
                (int(step["rate"]), step["phase"]),
            )
        )

    aligned: dict[tuple[int, str], list[DbCpuCoreSample]] = {key: [] for _, _, key in windows}
    for sample in samples:
        for start, end, key in windows:
            if start <= sample.timestamp <= end:
                # Primeiro degrau que casa vence: janelas vizinhas podem
                # encostar no mesmo instante, e contar a amostra duas vezes
                # inflaria a CPU média de ambos.
                aligned[key].append(sample)
                break
    return aligned
