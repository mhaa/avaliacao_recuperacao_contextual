"""Busca de vazão de saturação (CONTEXTO.md, "Protocolo de medição" / "3ª
dimensão da fronteira de Pareto") — patamares dobrando (rampa curta,
triagem) ou incrementos de 10% (rampa de confirmação), busca binária de até
3 iterações ao violar o SLO, teto de 50.000 req/s, censura quando o teto é
alcançado sem violação, e checagem obrigatória do gerador de carga (CPU <
60%, CONTEXTO.md) a cada patamar — se o gerador saturar antes da célula, a
execução inteira é inválida (`loadgen_bottleneck=True`), nunca interpretada
como vazão da célula.

Lógica pura, sem I/O de rede: quem chama fecha sobre a execução real de um
patamar (infra/scripts/run_measurement_battery.py) e passa aqui só como
`probe_fn(rate) -> ProbeResult`. Isso mantém o algoritmo de decisão
testável com um probe_fn falso, mesma disciplina de
storage/tests/fakes.py — nunca precisa de rede/gcloud real para testar a
lógica de busca."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator

GENERATOR_CPU_THRESHOLD = 60.0  # CONTEXTO.md: "válido só se CPU do gerador < 60%"
CEILING_RPS = 50_000  # teto da busca — decisão do usuário para este protocolo
DEFAULT_START_RATE = 1_000  # nível intermediário, mesmo da carga fixa da triagem
BINARY_SEARCH_ITERATIONS = 3


@dataclass(frozen=True)
class ProbeResult:
    rate: int
    violated_slo: bool
    generator_cpu_percent: float


@dataclass(frozen=True)
class SaturationSearchResult:
    approx_throughput: float | None  # None se censurado ou gerador saturou
    censored: bool
    lower_bound: float | None  # só quando censurado (= teto alcançado)
    loadgen_bottleneck: bool  # execução inválida — nunca usar como dado
    probes: list[ProbeResult] = field(default_factory=list)  # trilha de auditoria


def doubling_sequence(start: int, ceiling: int = CEILING_RPS) -> Iterator[int]:
    """1000, 2000, 4000, ... dobrando; o último patamar antes do teto é
    truncado para o teto exato em vez de ultrapassá-lo (rampa curta,
    triagem)."""
    rate = start
    while rate < ceiling:
        yield rate
        rate *= 2
    yield ceiling


def fine_sequence(start: int, ceiling: int = CEILING_RPS, step: float = 0.10) -> Iterator[int]:
    """Incrementos de 10% a partir de um valor aproximado já conhecido
    (rampa de confirmação, refinando o resultado da triagem — ou, se a
    triagem censurou a célula, a partir do `lower_bound`); mesmo
    truncamento no teto."""
    rate = float(start)
    while rate < ceiling:
        yield round(rate)
        rate *= 1 + step
    yield ceiling


def _binary_search(
    probe_fn: Callable[[int], ProbeResult],
    low: int,
    high: int,
    iterations: int,
    probes: list[ProbeResult],
) -> int:
    """`low` nunca violou o SLO, `high` violou. Sonda o ponto médio até
    `iterations` vezes, estreitando o intervalo; retorna o maior rate
    confirmado sem violação. Para cedo se o gerador saturar em qualquer
    sondagem — o chamador confere isso olhando `probes[-1]` depois."""
    for _ in range(iterations):
        mid = (low + high) // 2
        if mid <= low or mid >= high:
            break
        result = probe_fn(mid)
        probes.append(result)
        if result.generator_cpu_percent >= GENERATOR_CPU_THRESHOLD:
            break
        if result.violated_slo:
            high = mid
        else:
            low = mid
    return low


def run_saturation_search(
    probe_fn: Callable[[int], ProbeResult],
    start_rate: int = DEFAULT_START_RATE,
    ceiling: int = CEILING_RPS,
    binary_search_iterations: int = BINARY_SEARCH_ITERATIONS,
    step_mode: str = "doubling",
) -> SaturationSearchResult:
    """Roda a busca completa: sonda `doubling_sequence`/`fine_sequence`
    (conforme `step_mode`) a partir de `start_rate`. Se o gerador saturar em
    qualquer patamar: para imediatamente, `loadgen_bottleneck=True` (nunca
    interpretar como vazão da célula). Se violar o SLO: busca binária entre
    o último patamar válido e o que violou. Se a sequência inteira chegar ao
    teto sem violar: `censored=True`, `approx_throughput=None`,
    `lower_bound=ceiling`. O mesmo algoritmo serve para a rampa curta
    (`step_mode="doubling"`) e para a de confirmação
    (`step_mode="fine"`, `start_rate`= aproximado da triagem, ou seu
    `lower_bound` se censurada)."""
    sequence = (
        doubling_sequence(start_rate, ceiling)
        if step_mode == "doubling"
        else fine_sequence(start_rate, ceiling)
    )
    probes: list[ProbeResult] = []
    last_valid = 0

    for rate in sequence:
        result = probe_fn(rate)
        probes.append(result)

        if result.generator_cpu_percent >= GENERATOR_CPU_THRESHOLD:
            return SaturationSearchResult(
                approx_throughput=None,
                censored=False,
                lower_bound=None,
                loadgen_bottleneck=True,
                probes=probes,
            )

        if result.violated_slo:
            approx = _binary_search(probe_fn, last_valid, rate, binary_search_iterations, probes)
            if probes[-1].generator_cpu_percent >= GENERATOR_CPU_THRESHOLD:
                return SaturationSearchResult(
                    approx_throughput=None,
                    censored=False,
                    lower_bound=None,
                    loadgen_bottleneck=True,
                    probes=probes,
                )
            return SaturationSearchResult(
                approx_throughput=float(approx),
                censored=False,
                lower_bound=None,
                loadgen_bottleneck=False,
                probes=probes,
            )

        last_valid = rate

    return SaturationSearchResult(
        approx_throughput=None,
        censored=True,
        lower_bound=float(ceiling),
        loadgen_bottleneck=False,
        probes=probes,
    )
