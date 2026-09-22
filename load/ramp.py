"""Rampa de estresse com foco no banco (docs/DESIGN.md, "Experimento
complementar — estresse com foco no banco").

Diferença de contrato para `load/saturation.py`, e o motivo deste módulo
existir em vez de um parâmetro lá: `run_saturation_search` retorna na PRIMEIRA
violação do SLO — é o que se quer quando se busca `S`. Aqui o objetivo é o
oposto: atravessar o joelho, permanecer em sobrecarga, e voltar, para observar
se o banco se recupera sozinho ou se perde. Uma flag "não pare de verdade"
dentro daquela busca tornaria as duas intenções indistinguíveis no mesmo
código.

Cronograma ESTÁTICO, de propósito. A rampa roda como uma única execução do k6
(`ramping-arrival-rate` com estágios pareados), porque degraus separados por
reinícios de processo dariam ao banco janelas de ociosidade entre eles — e
drenar a fila entre degraus destruiria justamente o fenômeno sob observação.
Execução única exige que todos os estágios sejam conhecidos antes de começar,
então o alvo de sobrecarga vem de `knee * (1 + overshoot)` — a projeção do
joelho — e não de "10 degraus após a primeira violação", que só se saberia em
tempo de execução. Onde a violação de fato ocorreu é apurado depois, do dado
medido (`analysis/ramp_report.py`).

Lógica pura, sem I/O: mesma disciplina de `load/saturation.py` — quem chama
executa, aqui só se decide. Testável sem rede, gcloud ou k6.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from load.saturation import CEILING_RPS, GENERATOR_CPU_THRESHOLD

# Fases do cronograma. "plateau" é o único degrau cuja duração não é a do
# passo fino: é onde a sobrecarga é mantida tempo suficiente para o banco
# entrar em estado degradado (fila, compaction, GC) — sem isso, a descida
# mediria a recuperação de algo que nunca chegou a quebrar.
RampPhase = Literal["coarse_up", "fine_up", "plateau", "fine_down"]

DEFAULT_START_RATE = 1_000
DEFAULT_COARSE_STEP = 1_000
DEFAULT_COARSE_DURATION_S = 30
DEFAULT_FINE_DURATION_S = 60
DEFAULT_PLATEAU_DURATION_S = 300
# Fração do joelho projetado em que a fase grossa dá lugar à fina. Abaixo
# disso não há nada de interessante para resolver com passo fino, e cada
# degrau fino custa 1 min de VM.
DEFAULT_FINE_START_FRACTION = 0.6
# Quanto acima do joelho projetado a subida vai. 30% é o bastante para
# atravessar o joelho mesmo se a projeção errar para menos, sem gastar
# dezenas de degraus em falha profunda (a mesma lição que tirou os 10.000
# req/s fixos do nível "alto" — ver docs/DESIGN.md, "Protocolo de medição").
DEFAULT_OVERSHOOT = 0.30

# Medido, não estimado: results/e4-valkey/confirmacao/.../requests.ndjson tem
# 5.618.470 bytes em 42.002 linhas. Usado no pré-voo de disco — ver
# `estimated_ndjson_bytes`.
NDJSON_BYTES_PER_REQUEST = 134

# Fração do disco livre que o NDJSON previsto pode ocupar. Folga generosa
# porque a conta ignora o que mais cresce no disco durante a execução (logs
# do docker, camadas de imagem) e porque o modo de falha é silencioso: sem
# espaço, o k6 grava NDJSON truncado e o parse quebra depois — "no space left
# on device" seguido de JSONDecodeError, visto ao vivo em e2-opensearch
# (infra/modules/loadgen/main.tf).
DEFAULT_DISK_BUDGET_FRACTION = 0.5

# Tolerância relativa na comparação subida × descida. Abaixo dela, considera-se
# que a métrica voltou ao que era. 20% é folgado de propósito: a pergunta aqui
# é "voltou ao normal ou ficou preso?", e as assinaturas reais de não-recuperação
# são de ordem de grandeza (p99 de 131 ms subindo contra 3.987 ms descendo, na
# mesma taxa — results/e4-valkey/.../saturation_high.json), não de 20%.
DEFAULT_RECOVERY_TOLERANCE = 0.20


@dataclass(frozen=True)
class RampStep:
    """Um degrau do cronograma. `rate` é a carga OFERTADA; o que foi de fato
    sustentado só se sabe depois de medir (`RampStepResult.throughput_rps`)."""

    rate: int
    phase: RampPhase
    duration_s: int


@dataclass(frozen=True)
class RampStepResult:
    """Resultado medido de um degrau.

    Guarda os quatro percentis, e não só o p99 do veredito de SLO, porque a
    separação entre eles é o dado: sob sobrecarga o p50 costuma ficar quase
    parado enquanto o p99 explode, e é essa assimetria que docs/DESIGN.md usa
    para proibir médias.
    """

    rate: int
    phase: RampPhase
    throughput_rps: float | None = None
    latency_ms_p50: float | None = None
    latency_ms_p95: float | None = None
    latency_ms_p99: float | None = None
    latency_ms_p999: float | None = None
    error_rate: float | None = None
    request_count: int | None = None
    offered_ratio: float | None = None
    # False = o k6 esgotou maxVUs e descartou chegadas; os percentis deste
    # degrau são só dos sobreviventes (docs/DESIGN.md, "Vazão ofertada
    # verificada, não presumida"). Passado o joelho isso é esperado e é o
    # objeto de estudo — mas nunca pode ser lido como se o modelo aberto
    # tivesse se mantido, daí o campo viajar junto de cada degrau.
    offered_load_ok: bool | None = None
    violated_slo: bool | None = None
    # ISO-8601 UTC. Existem para alinhar resources.csv (30s) e
    # db_cpu_cores.csv (5s) a degraus — nenhum dos dois sabe qual degrau
    # estava ativo, e o join por intervalo acontece no host, na plotagem.
    started_at: str | None = None
    ended_at: str | None = None
    # Mesma semântica de load/saturation.py: None = não foi possível medir,
    # estado distinto de 0.0 = medido e ocioso.
    generator_cpu_percent: float | None = None


@dataclass(frozen=True)
class RecoveryComparison:
    """Um par (subida, descida) na mesma taxa ofertada."""

    rate: int
    metric: str
    ascent_value: float
    descent_value: float
    within_tolerance: bool


@dataclass(frozen=True)
class RecoveryVerdict:
    verdict: Literal["recovered", "lost", "undetermined"]
    comparisons: list[RecoveryComparison] = field(default_factory=list)
    # Por que "undetermined", quando for o caso. Evita que ausência de
    # evidência (nenhum par comparável) seja lida como evidência de ausência.
    reason: str | None = None


def build_step_schedule(
    knee: int,
    fine_step: int,
    *,
    start_rate: int = DEFAULT_START_RATE,
    coarse_step: int = DEFAULT_COARSE_STEP,
    coarse_duration_s: int = DEFAULT_COARSE_DURATION_S,
    fine_duration_s: int = DEFAULT_FINE_DURATION_S,
    plateau_duration_s: int = DEFAULT_PLATEAU_DURATION_S,
    fine_start_fraction: float = DEFAULT_FINE_START_FRACTION,
    overshoot: float = DEFAULT_OVERSHOOT,
    ceiling: int = CEILING_RPS,
) -> list[RampStep]:
    """Subida grossa → subida fina → patamar em sobrecarga → descida fina.

    A descida repete exatamente as taxas da subida fina, em ordem inversa: é o
    que torna a comparação por par possível em `classify_recovery`. Sem taxas
    coincidentes não há histerese a medir, só duas curvas soltas.
    """
    if knee <= 0:
        raise ValueError(f"joelho projetado precisa ser positivo: {knee}")
    if fine_step <= 0:
        raise ValueError(f"passo fino precisa ser positivo: {fine_step}")
    if not 0 < fine_start_fraction < 1:
        raise ValueError(f"fine_start_fraction precisa estar em (0, 1): {fine_start_fraction}")

    top = min(int(round(knee * (1 + overshoot))), ceiling)
    fine_start = min(int(round(knee * fine_start_fraction)), top)

    schedule: list[RampStep] = []

    # Fase grossa: só serve para chegar rápido à região de interesse. Começa
    # em start_rate e para ANTES de fine_start — o primeiro degrau fino é que
    # cobre fine_start.
    rate = start_rate
    while rate < fine_start:
        schedule.append(RampStep(rate, "coarse_up", coarse_duration_s))
        rate += coarse_step

    fine_rates: list[int] = []
    rate = fine_start
    while rate <= top:
        fine_rates.append(rate)
        rate += fine_step
    # Garante que o topo seja efetivamente oferecido, mesmo quando o passo
    # fino não cai exatamente nele.
    if not fine_rates:
        fine_rates = [top]
    elif fine_rates[-1] != top:
        fine_rates.append(top)

    schedule.extend(RampStep(r, "fine_up", fine_duration_s) for r in fine_rates)
    schedule.append(RampStep(fine_rates[-1], "plateau", plateau_duration_s))
    # Exclui o topo na descida: ele acabou de ser mantido no patamar, e
    # repeti-lo só somaria um degrau sem par de comparação novo.
    schedule.extend(RampStep(r, "fine_down", fine_duration_s) for r in reversed(fine_rates[:-1]))

    return schedule


def estimated_request_count(schedule: list[RampStep]) -> int:
    """Σ(taxa × duração) — o total de requisições que a rampa vai emitir se
    toda a carga ofertada for de fato entregue. É um teto: passado o joelho o
    k6 descarta chegadas, então o volume real tende a ser menor. Teto é o que
    interessa para dimensionar disco."""
    return sum(step.rate * step.duration_s for step in schedule)


def estimated_ndjson_bytes(schedule: list[RampStep]) -> int:
    return estimated_request_count(schedule) * NDJSON_BYTES_PER_REQUEST


def check_disk_budget(
    schedule: list[RampStep],
    free_bytes: int,
    *,
    budget_fraction: float = DEFAULT_DISK_BUDGET_FRACTION,
) -> None:
    """Pré-voo determinístico: falha ANTES de provisionar qualquer VM.

    Existe porque a proteção atual do disco — upload e `unlink` do
    `k6-raw.json` a cada repetição — nunca dispara numa rampa contínua, que
    tem uma única execução do k6. O modo de falha sem isso é silencioso e
    caro: NDJSON truncado, descoberto só no parse, depois de a campanha
    inteira ter sido paga.
    """
    needed = estimated_ndjson_bytes(schedule)
    budget = int(free_bytes * budget_fraction)
    if needed > budget:
        raise ValueError(
            f"NDJSON previsto ({needed / 1e9:.1f} GB) excede {budget_fraction:.0%} do disco "
            f"livre ({free_bytes / 1e9:.1f} GB). Reduza a rampa (--ramp-fine-step maior, "
            f"--ramp-knee menor) ou aumente o disco do gerador "
            f"(infra/modules/loadgen/main.tf)."
        )


def generator_saturated(result: RampStepResult) -> bool:
    """Mesmo portão de `load/saturation.py:_generator_saturated`, com a mesma
    semântica de três estados: leitura acima do limiar é gargalo; `None` é
    "não medido", nunca "ocioso".

    Diferença de consequência: aqui não aborta a busca (não há busca), mas
    marca o degrau como inválido — a curva naquele ponto é do gerador, não do
    banco.
    """
    return (
        result.generator_cpu_percent is not None
        and result.generator_cpu_percent >= GENERATOR_CPU_THRESHOLD
    )


_RECOVERY_METRICS = (
    ("throughput_rps", False),  # maior é melhor
    ("latency_ms_p50", True),  # menor é melhor
    ("latency_ms_p95", True),
    ("latency_ms_p99", True),
)


def _degraded(ascent: float, descent: float, lower_is_better: bool, tolerance: float) -> bool:
    if ascent <= 0:
        return False
    relative = (descent - ascent) / ascent
    return relative > tolerance if lower_is_better else -relative > tolerance


def classify_recovery(
    results: list[RampStepResult],
    *,
    tolerance: float = DEFAULT_RECOVERY_TOLERANCE,
) -> RecoveryVerdict:
    """Histerese: compara degraus de MESMA taxa na subida e na descida.

    Só entram pares cujo degrau de SUBIDA estava saudável (`offered_load_ok`
    e sem violação de SLO). Comparar contra um degrau de subida que já estava
    degradado não diz nada sobre recuperação — os dois lados estariam ruins.

    Avaliar por percentil, e não só pelo p99, separa dois desfechos que um
    número agregado confunde: "voltou ao normal" e "a vazão voltou mas a
    cauda ficou presa".
    """
    ascent = {r.rate: r for r in results if r.phase in ("coarse_up", "fine_up")}
    descent = {r.rate: r for r in results if r.phase == "fine_down"}

    comparisons: list[RecoveryComparison] = []
    for rate in sorted(set(ascent) & set(descent)):
        up, down = ascent[rate], descent[rate]
        if up.offered_load_ok is not True or up.violated_slo is not False:
            continue
        for metric, lower_is_better in _RECOVERY_METRICS:
            up_value, down_value = getattr(up, metric), getattr(down, metric)
            if up_value is None or down_value is None:
                continue
            comparisons.append(
                RecoveryComparison(
                    rate=rate,
                    metric=metric,
                    ascent_value=up_value,
                    descent_value=down_value,
                    within_tolerance=not _degraded(
                        up_value, down_value, lower_is_better, tolerance
                    ),
                )
            )

    if not comparisons:
        return RecoveryVerdict(
            verdict="undetermined",
            reason=(
                "nenhum par subida/descida na mesma taxa com o degrau de subida saudável "
                "(offered_load_ok e sem violação de SLO)"
            ),
        )
    if all(c.within_tolerance for c in comparisons):
        return RecoveryVerdict(verdict="recovered", comparisons=comparisons)
    return RecoveryVerdict(verdict="lost", comparisons=comparisons)
