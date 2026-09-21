"""Busca de vazão de saturação (docs/DESIGN.md, "Protocolo de medição").

`S` não é mais uma dimensão própria da fronteira de Pareto: ele entra no
custo, via `n(D) = ⌈D/S⌉` (analysis/pareto.py). Isso elevou a exigência sobre
a precisão desta busca — um erro em `S` vira erro no custo — e é o motivo do
protocolo atual:

- patamares em incrementos finos (rampa curta da triagem: 25%; rampa de
  confirmação: 10%) em vez de dobras, para o ponto de violação cair mais
  perto do valor real;
- busca binária de até 5 iterações ao violar o SLO. São elas, não os passos
  finos, que determinam a resolução das células que saturam ABAIXO do rate
  inicial (violam já na primeira sondagem e a busca desce de 0 até lá);
- `confirm_repetitions` re-sonda o patamar aprovado, para o `S` reportado ter
  dispersão em vez de ser ensaio único;
- teto de 50.000 req/s, com censura quando alcançado sem violação.

Checagem obrigatória do gerador de carga (CPU < 60%, docs/DESIGN.md) a cada
patamar — se o gerador saturar antes da célula durante a BUSCA, a execução
inteira é inválida (`loadgen_bottleneck=True`), nunca interpretada como vazão
da célula. Uma sondagem SEM leitura de CPU (`generator_cpu_percent=None`) não
aborta a busca, mas marca `generator_cpu_unmeasured=True`: "não deu pra
avaliar o portão" é estado próprio, nunca confundido com "gerador ocioso".

Lógica pura, sem I/O de rede: quem chama fecha sobre a execução real de um
patamar (infra/scripts/run_measurement_battery.py) e passa aqui só como
`probe_fn(rate) -> ProbeResult`. Isso mantém o algoritmo de decisão
testável com um probe_fn falso, mesma disciplina de
storage/tests/fakes.py — nunca precisa de rede/gcloud real para testar a
lógica de busca."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator

GENERATOR_CPU_THRESHOLD = 60.0  # docs/DESIGN.md: "válido só se CPU do gerador < 60%"
CEILING_RPS = 50_000  # teto da busca — decisão do usuário para este protocolo
DEFAULT_START_RATE = 1_000  # nível intermediário, mesmo da carga fixa da triagem
BINARY_SEARCH_ITERATIONS = 5
BACKWARD_WALK_MAX_STEPS = 10


@dataclass(frozen=True)
class ProbeResult:
    rate: int
    violated_slo: bool
    # `None` = não foi POSSÍVEL medir a CPU do gerador nesta sondagem, estado
    # distinto de 0.0 = medido e ocioso. Desde que a CPU passou a vir de
    # /proc/stat lido na própria VM loadgen (analysis/probe_report.py:
    # _cpu_percent_from_stat, repassado por infra/scripts/
    # run_measurement_battery.py:_parse_probe_result), `None` deveria ser
    # raríssimo — mas o campo continua opcional como rede de segurança: antes,
    # quando a CPU vinha do Cloud Monitoring, uma falha de telemetria virava
    # 0.0 e passava calada pelo portão dos 60% do docs/DESIGN.md
    # (results/e1-postgres/triagem/20260901T144228Z/saturation.json tem 0.0
    # nas 4 sondagens enquanto o gerador empurrava 1000 req/s — implausível).
    # Mesma disciplina de analysis/resources.py:classify_bottleneck (ausente
    # != zero).
    generator_cpu_percent: float | None
    p99_ms: float | None = None
    error_rate: float | None = None
    # request_count / esperado da sondagem (analysis/probe_report.py). Abaixo
    # de analysis/collect.py:MIN_OFFERED_RATIO o veredito violated_slo já vem
    # True de lá — o valor fica aqui só como trilha de auditoria em
    # saturation.json (por que ESTA sondagem violou: SLO ou déficit de
    # oferta). None em sondagens antigas, sem --expected-requests.
    offered_ratio: float | None = None
    # Trilha de auditoria de --decision-statistic median-per-repetition
    # (analysis/probe_report.py): o p99/veredito de CADA repetição
    # individual, não só o valor agregado (mediana) usado para decidir
    # violated_slo. None em sondagens no modo "pooled" (default) ou em
    # sondagens antigas, sem esses campos na saída do probe. Existe porque
    # o modo "pooled" pode mascarar uma repetição isolada que viola o SLO
    # dentro de um agregado que não viola — ver docs/DESIGN.md, a
    # subseção sobre re-medição de seletividade média.
    per_rep_p99_ms: list[float] | None = None
    per_rep_violated_slo: list[bool] | None = None


@dataclass(frozen=True)
class SaturationSearchResult:
    approx_throughput: float | None  # None se censurado ou gerador saturou
    censored: bool
    lower_bound: float | None  # só quando censurado (= teto alcançado)
    loadgen_bottleneck: bool  # execução inválida — nunca usar como dado
    # True se ao menos uma sondagem ficou sem leitura de CPU do gerador: o
    # resultado NÃO é inválido, mas também não está validado — o portão dos
    # 60% não pôde ser avaliado naquelas sondagens. Quem lê decide.
    generator_cpu_unmeasured: bool = False
    probes: list[ProbeResult] = field(default_factory=list)  # trilha de auditoria
    # Repetições do patamar final aprovado (ver `confirm_repetitions` em
    # run_saturation_search). Existe para o S reportado deixar de ser ensaio
    # único: com elas dá para reportar dispersão do p99 e quantas repetições
    # violaram o SLO naquele mesmo patamar. Vazia quando a confirmação não foi
    # pedida, ou quando a célula ficou censurada/inválida.
    final_level_probes: list[ProbeResult] = field(default_factory=list)


def _generator_saturated(result: ProbeResult) -> bool:
    """Portão de validade do docs/DESIGN.md (CPU do gerador < 60%): só dispara
    com uma LEITURA acima do limiar. `None` não é gargalo confirmado e não
    aborta a busca (rede de segurança — ver o comentário de
    ProbeResult.generator_cpu_percent; a CPU normalmente já chega medida,
    lida de /proc/stat por analysis/probe_report.py). Fica registrado em
    SaturationSearchResult.generator_cpu_unmeasured: não medido é visível,
    não fatal."""
    return (
        result.generator_cpu_percent is not None
        and result.generator_cpu_percent >= GENERATOR_CPU_THRESHOLD
    )


def _any_cpu_unmeasured(probes: list[ProbeResult]) -> bool:
    return any(p.generator_cpu_percent is None for p in probes)


def _bottleneck_result(probes: list[ProbeResult]) -> SaturationSearchResult:
    return SaturationSearchResult(
        approx_throughput=None,
        censored=False,
        lower_bound=None,
        loadgen_bottleneck=True,
        generator_cpu_unmeasured=_any_cpu_unmeasured(probes),
        probes=probes,
    )


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
    min_step: int | None = None,
) -> int:
    """`low` nunca violou o SLO, `high` violou. Sonda o ponto médio,
    estreitando o intervalo; retorna o maior rate confirmado sem violação.
    Para cedo se o gerador saturar em qualquer sondagem — o chamador
    confere isso olhando `probes[-1]` depois.

    Duas condições de parada, mutuamente exclusivas por chamada:
    - `min_step=None` (default, usado pela triagem): para depois de
      exatamente `iterations` sondagens, não importa a largura final do
      intervalo — resolução previsível, custo previsível.
    - `min_step` informado (rampa de confirmação re-medida com precisão
      alvo, docs/DESIGN.md): para quando `high - low <= min_step`, não
      importa quantas sondagens isso levou — `iterations` continua valendo
      como TETO de segurança (nunca ilimitado: um veredito instável/
      oscilante não pode travar a busca para sempre)."""
    for _ in range(iterations):
        if min_step is not None and high - low <= min_step:
            break
        mid = (low + high) // 2
        if mid <= low or mid >= high:
            break
        result = probe_fn(mid)
        probes.append(result)
        if _generator_saturated(result):
            break
        if result.violated_slo:
            high = mid
        else:
            low = mid
    return low


def _backward_walk_to_bracket(
    probe_fn: Callable[[int], ProbeResult],
    start_high: int,
    step: float,
    probes: list[ProbeResult],
    max_steps: int = BACKWARD_WALK_MAX_STEPS,
) -> tuple[int, int]:
    """Quando a PRIMEIRA sondagem da rampa já viola o SLO, `last_valid`
    continua no sentinela 0 e a busca binária receberia o bracket inteiro
    [0, start] — caro com `binary_search_min_step`, cujo número de
    iterações escala com a largura do bracket (docs/DESIGN.md: achado ao
    vivo re-medindo seletividade média, ponto de partida vindo da triagem
    antiga — pooled, possivelmente otimista — que pode já vir violando sob
    `median-per-repetition`).

    Anda para trás na MESMA grade geométrica de `fine_sequence` (÷ (1+step)
    a cada passo) até achar um patamar que não viola, devolvendo um
    bracket estreito (~`step` de largura) para a busca binária, em vez de
    um bracket [0, start] largo. Teto de `max_steps`: rede de segurança
    contra uma célula que satura mesmo perto de zero — nesse caso desiste
    e devolve low=0 (equivalente ao comportamento anterior a esta função),
    com `high` ainda tão apertado quanto o recuo conseguiu chegar."""
    high = start_high
    for _ in range(max_steps):
        low = round(high / (1 + step))
        if low <= 0:
            return 0, high
        result = probe_fn(low)
        probes.append(result)
        if _generator_saturated(result):
            return low, high
        if not result.violated_slo:
            return low, high
        high = low
    return 0, high


def _confirm_final_level(
    probe_fn: Callable[[int], ProbeResult],
    rate: int,
    repetitions: int,
    probes: list[ProbeResult],
) -> list[ProbeResult]:
    """Re-sonda `repetitions` vezes o patamar já aprovado pela busca.

    Motivo: sem isso o `S` reportado vem de UMA sondagem, sem estimativa de
    variância — e ele entra direto em `n(D) = ⌈D/S⌉`, ou seja, no custo. As
    repetições não alteram o valor aproximado (mudar o resultado com base
    nelas seria refazer a busca pela metade); elas ficam registradas para que
    a análise possa reportar dispersão e quantas violaram o SLO no mesmo
    patamar. Para cedo se o gerador saturar — nesse caso o problema é o
    gerador, não a célula."""
    confirmations: list[ProbeResult] = []
    for _ in range(repetitions):
        result = probe_fn(rate)
        probes.append(result)
        confirmations.append(result)
        if _generator_saturated(result):
            break
    return confirmations


def run_saturation_search(
    probe_fn: Callable[[int], ProbeResult],
    start_rate: int = DEFAULT_START_RATE,
    ceiling: int = CEILING_RPS,
    binary_search_iterations: int = BINARY_SEARCH_ITERATIONS,
    step_mode: str = "doubling",
    step: float = 0.10,
    confirm_repetitions: int = 0,
    binary_search_min_step: int | None = None,
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
    `lower_bound` se censurada).

    `binary_search_min_step`: repassado a `_binary_search` — quando
    informado, a busca binária para por LARGURA de intervalo (útil quando
    se quer um `S` com precisão-alvo em req/s, não um número fixo de
    sondagens); `binary_search_iterations` continua valendo como teto de
    segurança nesse modo, então o chamador deve passar um valor generoso o
    bastante para a largura pedida caber (ver docs/DESIGN.md). Nesse mesmo
    modo, se a PRIMEIRA sondagem da rampa já violar o SLO (`start_rate`
    otimista, ex. vindo de uma triagem medida pelo método `pooled`
    antigo), a busca recua em passos de `step` (`_backward_walk_to_bracket`)
    antes de entrar na busca binária — sem isso o bracket ficaria [0,
    start_rate] inteiro, custando iterações binárias demais."""
    sequence = (
        doubling_sequence(start_rate, ceiling)
        if step_mode == "doubling"
        else fine_sequence(start_rate, ceiling, step)
    )
    probes: list[ProbeResult] = []
    last_valid = 0

    for rate in sequence:
        result = probe_fn(rate)
        probes.append(result)

        if _generator_saturated(result):
            return _bottleneck_result(probes)

        if result.violated_slo:
            search_low, search_high = last_valid, rate
            # Primeira sondagem da rampa já violando (last_valid ainda no
            # sentinela 0): sem isso a busca binária receberia [0, rate]
            # inteiro. Só compensa recuar quando min_step está ativo — no
            # modo de iterações fixas (triagem, ou confirmação sem
            # --saturation-min-step) a largura do bracket não muda o custo
            # da busca binária, então recuar só somaria sondagens extras
            # sem nenhum ganho.
            if last_valid == 0 and binary_search_min_step is not None:
                search_low, search_high = _backward_walk_to_bracket(probe_fn, rate, step, probes)
                if _generator_saturated(probes[-1]):
                    return _bottleneck_result(probes)
            approx = _binary_search(
                probe_fn,
                search_low,
                search_high,
                binary_search_iterations,
                probes,
                min_step=binary_search_min_step,
            )
            if _generator_saturated(probes[-1]):
                return _bottleneck_result(probes)
            # Confirmação só do patamar aprovado, e só quando há um: uma
            # célula censurada não tem patamar de violação para repetir (e o
            # custo dela nem usa S como valor pontual — ⌈D/S⌉ = 1 sai da
            # própria desigualdade S ≥ teto).
            final_level = (
                _confirm_final_level(probe_fn, approx, confirm_repetitions, probes)
                if confirm_repetitions > 0 and approx > 0
                else []
            )
            return SaturationSearchResult(
                approx_throughput=float(approx),
                censored=False,
                lower_bound=None,
                # `loadgen_bottleneck` segue False mesmo se o gerador saturar
                # DURANTE a confirmação: a busca já terminou e o `approx` dela
                # continua válido — o que se perde é só parte da repetição.
                # Marcar a execução inteira como inválida aqui contradiria o
                # próprio approx_throughput que estamos devolvendo; a lista
                # `final_level_probes` mais curta que o pedido é o sinal.
                loadgen_bottleneck=False,
                generator_cpu_unmeasured=_any_cpu_unmeasured(probes),
                probes=probes,
                final_level_probes=final_level,
            )

        last_valid = rate

    return SaturationSearchResult(
        approx_throughput=None,
        censored=True,
        lower_bound=float(ceiling),
        loadgen_bottleneck=False,
        generator_cpu_unmeasured=_any_cpu_unmeasured(probes),
        probes=probes,
    )
