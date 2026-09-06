"""Dominância de Pareto em 2 dimensões — latência p99 × custo(D).

A vazão de saturação NÃO é um terceiro eixo. Ela é internalizada no custo:
`S` determina quantas unidades de atendimento são necessárias para servir uma
demanda `D`, e é o número de unidades que se paga. Manter vazão como eixo
separado, com o custo já dependendo dela, contaria a mesma grandeza duas vezes
(ver docs/DESIGN.md, "Custo como função da demanda e pontos de cruzamento").

Modelo (docs/DESIGN.md):

    n(D)   = máx( ⌈D / S⌉ , ⌈V_mem / M⌉ )
    C_f(D) = n(D) · p_i · h                    [fluxo: computação]
    C_a(D) = n(D) · V_disco · p_a              [estoque: armazenamento em disco]
    C(D)   = C_f(D) + C_a(D) = n(D) · custo_por_unidade

O termo de capacidade `⌈V_mem / M⌉` é o que distingue memória de disco: disco é
elástico e faturado por GiB (entra em `C_a`), memória já está paga dentro de
`p_i` e portanto não tem preço próprio — ela limita quantas unidades cabem. As
duas parcelas estão consolidadas em `unit_cost_usd_month`, calculado por
analysis/report.py; aqui só se multiplica por `n(D)`.

Como `C` depende de `D`, a fronteira também depende: `pareto_frontier` exige uma
demanda. `frontier_segments` varre `(0, D_max]` e devolve as faixas maximais em
que a fronteira e a configuração mais barata não mudam — as fronteiras entre
essas faixas são exatamente os pontos de cruzamento que o trabalho procura.

Aritmética em `fractions.Fraction`, não `float`: `S·(1±TOL)` não é diádico
(1,2 não tem representação exata em binário), e `math.ceil(D/q)` em `D = m·q`
pode devolver `m+1` quando `D` e `q` chegam por caminhos de cálculo diferentes
— o que deslocaria um cruzamento em um segmento inteiro, silenciosamente.
"""

from __future__ import annotations

import math
from fractions import Fraction

# docs/DESIGN.md: a rampa curta é ensaio único, sem estimativa de variância.
# A tolerância não compara vazões (não há mais eixo de vazão) — ela propaga a
# incerteza de `S` para o número de unidades, alargando `n` num intervalo.
# Incide SÓ sobre `S`, que é medido; nunca sobre o termo de capacidade de
# memória nem sobre o limite inferior de uma célula censurada, que são exatos.
TOLERANCE = 0.20


def _as_fraction(value) -> Fraction:
    """Converte pela intenção DECIMAL do valor, não pelo binário do float.

    `Fraction(0.2)` devolve 3602879701896397/18014398509481984 — o binário
    exato do float, que não é 1/5. A consequência é concreta e foi pega por
    teste: `1000·(1−0,2)·3` e `1000·(1+0,2)·2` viram racionais diferentes,
    então o conjunto de breakpoints ganha DOIS pontos distintos que ambos
    imprimem como "2400", partindo um segmento que deveria ser único.
    `str()` usa a repr mais curta que faz round-trip, então
    `Fraction(str(0.2))` é exatamente 1/5."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, float):
        return Fraction(str(value))
    return Fraction(value)


def capacity_units(cell: dict) -> int:
    """`⌈V_mem / M⌉` — quantas unidades a base exige só para caber em memória.

    Vale 1 para mecanismos em disco (`memory_bytes` ausente ou zero), deixando
    o `máx` de `units_at` inerte. Independe de `D`: por isso nunca gera ponto
    de cruzamento."""
    memory_bytes = cell.get("memory_bytes") or 0
    if memory_bytes <= 0:
        return 1
    per_unit = cell.get("memory_per_unit_bytes") or 0
    if per_unit <= 0:
        # Erro de programação, não condição de dado: uma célula que declara
        # volume em memória sem declarar a memória útil por unidade
        # subestimaria o custo silenciosamente.
        raise ValueError(
            f"{cell.get('cell_id')}: memory_bytes={memory_bytes} sem "
            "memory_per_unit_bytes — impossível calcular o termo de capacidade."
        )
    return math.ceil(Fraction(int(memory_bytes), int(per_unit)))


def _throughput_units(cell: dict, demand: Fraction, scale: Fraction = Fraction(1)) -> int | None:
    """`⌈D / (S·scale)⌉`, ou None quando `S` é desconhecido àquela demanda.

    Censuradas: o fato medido é `S ≥ L`, nada mais. Disso sai
    `1 ≤ n(D) ≤ ⌈D/L⌉`; para qualquer `D ≤ L` temos `⌈D/L⌉ = 1`, logo `n = 1`
    **exatamente** — a desigualdade sozinha resolve, sem nunca usar o teto como
    se fosse o `S` medido (proibido por docs/DESIGN.md). Acima de `L` a célula é
    genuinamente indeterminada, e devolver None é o honesto.

    `scale` existe para a banda de tolerância e por isso NÃO se aplica ao caso
    censurado: `L` é um limite, não estimativa pontual — alargá-lo seria
    inventar incerteza sobre um fato."""
    if cell.get("saturation_censored", False):
        lower_bound = cell.get("saturation_lower_bound")
        if lower_bound is None:
            return None
        return 1 if demand <= _as_fraction(lower_bound) else None

    saturation = cell.get("saturation_throughput_approx")
    if saturation is None or saturation <= 0:
        return None
    return math.ceil(demand / (_as_fraction(saturation) * scale))


def units_at(cell: dict, demand) -> int | None:
    """Unidades de atendimento necessárias em `demand`. None se indeterminado."""
    throughput_units = _throughput_units(cell, _as_fraction(demand))
    if throughput_units is None:
        return None
    return max(throughput_units, capacity_units(cell))


def units_bounds_at(cell: dict, demand, tolerance: float = TOLERANCE) -> tuple[int, int] | None:
    """`(n_lo, n_hi)` propagando ±`tolerance` sobre `S`. None se indeterminado.

    Propriedade que torna isso correto: quando ambos os extremos dão 1 unidade
    (`D ≤ 0,8·S`), o intervalo degenera num ponto e as diferenças pequenas de
    armazenamento voltam a discriminar exatamente. A tolerância só borra onde
    existe incerteza de verdade."""
    demand = _as_fraction(demand)
    tolerance = _as_fraction(tolerance)
    floor_units = capacity_units(cell)
    lower = _throughput_units(cell, demand, scale=Fraction(1) + tolerance)
    upper = _throughput_units(cell, demand, scale=Fraction(1) - tolerance)
    if lower is None or upper is None:
        return None
    return (max(lower, floor_units), max(upper, floor_units))


def cost_at(cell: dict, demand) -> float | None:
    units = units_at(cell, demand)
    unit_cost = cell.get("unit_cost_usd_month")
    if units is None or unit_cost is None:
        return None
    return units * unit_cost


def cost_bounds_at(cell: dict, demand, tolerance: float = TOLERANCE) -> tuple[float, float] | None:
    bounds = units_bounds_at(cell, demand, tolerance)
    unit_cost = cell.get("unit_cost_usd_month")
    if bounds is None or unit_cost is None:
        return None
    return (bounds[0] * unit_cost, bounds[1] * unit_cost)


def cost_curve(cell: dict, demand_max) -> list[dict]:
    """Degraus de `C(D)` sobre `(0, demand_max]` — para o gráfico em degraus.

    Cada degrau é `(demand_from, demand_to]` com `units` e custo constantes.
    `tolerance=0` de propósito: a curva desenhada é a estimativa pontual, não a
    banda (a banda entra na dominância, não no gráfico de custo)."""
    demand_max = _as_fraction(demand_max)
    steps: list[dict] = []
    if demand_max <= 0:
        return steps

    edges = [b for b in breakpoints([cell], demand_max, tolerance=0.0) if 0 < b < demand_max]
    start = Fraction(0)
    for end in [*edges, demand_max]:
        units = units_at(cell, end)
        if units is not None:
            steps.append(
                {
                    "demand_from_rps": float(start),
                    "demand_to_rps": float(end),
                    "units": units,
                    "cost_usd_month": units * cell["unit_cost_usd_month"],
                }
            )
        start = end
    return steps


def dominates(a: dict, b: dict, demand, tolerance: float = TOLERANCE) -> bool:
    """`a` domina `b` em `demand`: melhor-ou-igual nas 2 dimensões e
    estritamente melhor em ao menos uma.

    Custo compara intervalos (só domina se forem disjuntos, absorvendo a
    incerteza de `S`); latência compara valores diretos, sem tolerância — ela
    vem de 5 repetições com IC de bootstrap, não de um ensaio único.

    Custo indefinido nunca domina nem é dominado: uma célula sem `S` não pode
    ser colocada no plano, e fingir que pode a poria na fronteira de graça."""
    bounds_a = cost_bounds_at(a, demand, tolerance)
    bounds_b = cost_bounds_at(b, demand, tolerance)
    if bounds_a is None or bounds_b is None:
        return False

    cost_a_better = bounds_a[1] < bounds_b[0]
    cost_b_better = bounds_b[1] < bounds_a[0]
    latency_a_better = a["latency_p99_ms"] < b["latency_p99_ms"]
    latency_b_better = b["latency_p99_ms"] < a["latency_p99_ms"]

    better_or_equal = not cost_b_better and not latency_b_better
    strictly_better = cost_a_better or latency_a_better
    return better_or_equal and strictly_better


def pareto_frontier(cells: list[dict], demand, tolerance: float = TOLERANCE) -> list[dict]:
    """Células não dominadas em `demand`. Células sem custo definido ficam de
    fora (ver `cells_without_cost`)."""
    priced = [c for c in cells if cost_bounds_at(c, demand, tolerance) is not None]
    return [
        cell
        for cell in priced
        if not any(
            dominates(other, cell, demand, tolerance) for other in priced if other is not cell
        )
    ]


def cheapest(cells: list[dict], demand) -> dict | None:
    """Célula de menor `C(D)` pela ESTIMATIVA PONTUAL — argmin sobre intervalos
    seria mal definido. Desempate por `cell_id`, para ser determinístico."""
    priced = [(cost_at(c, demand), c) for c in cells]
    priced = [(cost, c) for cost, c in priced if cost is not None]
    if not priced:
        return None
    return min(priced, key=lambda pair: (pair[0], pair[1]["cell_id"]))[1]


def breakpoints(cells: list[dict], demand_max, tolerance: float = TOLERANCE) -> list[Fraction]:
    """Demandas candidatas a cruzamento: os múltiplos de `S` e das bordas da
    banda de tolerância.

    `C_k(D) = ⌈D/S_k⌉·U_k` é constante em `(m·S_k, (m+1)·S_k]` e salta logo à
    direita de `m·S_k`. Como a dominância consulta também `n_lo` e `n_hi`, a
    fronteira só pode mudar onde uma das três famílias salta — daí bastar
    enumerá-las, sem amostragem densa.

    Censuradas não contribuem (n = 1 constante no domínio) e o termo de
    capacidade tampouco (não depende de `D`)."""
    demand_max = _as_fraction(demand_max)
    tolerance = _as_fraction(tolerance)
    points: set[Fraction] = set()

    for cell in cells:
        if cell.get("saturation_censored", False):
            continue
        saturation = cell.get("saturation_throughput_approx")
        if saturation is None or saturation <= 0:
            continue
        saturation = _as_fraction(saturation)
        for scale in {Fraction(1), Fraction(1) + tolerance, Fraction(1) - tolerance}:
            step = saturation * scale
            if step <= 0:
                continue
            multiple = step
            while multiple < demand_max:
                points.add(multiple)
                multiple += step

    return sorted(points)


def frontier_segments(cells: list[dict], demand_max, tolerance: float = TOLERANCE) -> list[dict]:
    """Partição exata de `(0, demand_max]` em faixas maximais nas quais a
    fronteira e a configuração mais barata não mudam.

    Cada faixa é avaliada no seu extremo DIREITO: como o intervalo é fechado à
    direita, o extremo é representante interior legítimo — não precisa de
    epsilon nem de amostragem. `units_at_upper_bound` é, como o nome diz,
    medido nesse extremo: dentro de uma faixa fundida, uma célula dominada pode
    ter incrementado unidades sem alterar a decisão."""
    demand_max = _as_fraction(demand_max)
    if demand_max <= 0:
        return []

    edges = [b for b in breakpoints(cells, demand_max, tolerance) if 0 < b < demand_max]
    raw: list[dict] = []
    start = Fraction(0)
    for end in [*edges, demand_max]:
        frontier = pareto_frontier(cells, end, tolerance)
        cheapest_cell = cheapest(cells, end)
        raw.append(
            {
                "demand_from_rps_exclusive": float(start),
                "demand_to_rps_inclusive": float(end),
                "pareto_frontier": sorted(c["cell_id"] for c in frontier),
                "cheapest_cell_id": cheapest_cell["cell_id"] if cheapest_cell else None,
                "cheapest_cost_usd_month": cost_at(cheapest_cell, end) if cheapest_cell else None,
                "units_at_upper_bound": {
                    c["cell_id"]: units_at(c, end) for c in cells if units_at(c, end) is not None
                },
            }
        )
        start = end

    merged: list[dict] = []
    for segment in raw:
        previous = merged[-1] if merged else None
        same_decision = (
            previous is not None
            and previous["pareto_frontier"] == segment["pareto_frontier"]
            and previous["cheapest_cell_id"] == segment["cheapest_cell_id"]
        )
        if same_decision:
            previous["demand_to_rps_inclusive"] = segment["demand_to_rps_inclusive"]
            previous["cheapest_cost_usd_month"] = segment["cheapest_cost_usd_month"]
            previous["units_at_upper_bound"] = segment["units_at_upper_bound"]
        else:
            merged.append(segment)
    return merged


def _units_incremented_at(cells: list[dict], demand) -> list[str]:
    """Células cujo `S` divide exatamente `demand` — ou seja, que somaram uma
    unidade nesse ponto. É o que transforma "a fronteira mudou em 300 req/s" em
    "mudou porque e1-scylla passou a precisar de 2 unidades"."""
    demand = _as_fraction(demand)
    incremented = []
    for cell in cells:
        if cell.get("saturation_censored", False):
            continue
        saturation = cell.get("saturation_throughput_approx")
        if saturation is None or saturation <= 0:
            continue
        quotient = demand / _as_fraction(saturation)
        if quotient.denominator == 1 and quotient.numerator >= 1:
            incremented.append(cell["cell_id"])
    return sorted(incremented)


def crossovers(segments: list[dict], cells: list[dict] | None = None) -> dict:
    """Pontos em que a decisão muda, em duas famílias.

    `frontier`: o conjunto não dominado mudou — são os pontos de decisão
    arquitetural substantivos.

    `cost`: o argmin mudou. Vem com `relative_gap` e `within_tolerance`, porque
    uma troca de "mais barato" com diferença de fração de por cento não é
    achado nenhum: é ruído dentro da própria incerteza de `S`, e apresentá-la
    como resultado seria over-claiming."""
    cells = cells or []
    frontier_changes: list[dict] = []
    cost_changes: list[dict] = []

    for previous, current in zip(segments, segments[1:]):
        demand = current["demand_from_rps_exclusive"]
        incremented = _units_incremented_at(cells, demand) if cells else []

        before = set(previous["pareto_frontier"])
        after = set(current["pareto_frontier"])
        if before != after:
            frontier_changes.append(
                {
                    "demand_rps": demand,
                    "units_incremented": incremented,
                    "entered": sorted(after - before),
                    "left": sorted(before - after),
                    "frontier_before": previous["pareto_frontier"],
                    "frontier_after": current["pareto_frontier"],
                }
            )

        if previous["cheapest_cell_id"] != current["cheapest_cell_id"]:
            from_cost = previous["cheapest_cost_usd_month"]
            to_cost = current["cheapest_cost_usd_month"]
            relative_gap = abs(to_cost - from_cost) / from_cost if from_cost else None
            cost_changes.append(
                {
                    "demand_rps": demand,
                    "units_incremented": incremented,
                    "from_cell_id": previous["cheapest_cell_id"],
                    "to_cell_id": current["cheapest_cell_id"],
                    "from_cost_usd_month": from_cost,
                    "to_cost_usd_month": to_cost,
                    "relative_gap": relative_gap,
                    "within_tolerance": relative_gap is not None and relative_gap <= TOLERANCE,
                }
            )

    return {"frontier": frontier_changes, "cost": cost_changes}


def cost_undefined_reason(cell: dict) -> str | None:
    """Por que esta célula não pode ser posta no plano de custo, ou None."""
    if cell.get("saturation_censored", False):
        if cell.get("saturation_lower_bound") is None:
            return "censurada sem lower_bound registrado"
    else:
        saturation = cell.get("saturation_throughput_approx")
        if saturation is None:
            return "vazão de saturação sem dado (ex.: o gerador saturou antes da célula)"
        if saturation <= 0:
            return f"vazão de saturação inválida ({saturation})"
    if cell.get("unit_cost_usd_month") is None:
        return "custo por unidade não calculado (falta medição de armazenamento?)"
    return None


def cells_without_cost(cells: list[dict]) -> list[dict]:
    """Células excluídas do plano de custo, com o motivo — nunca silenciosas."""
    listed = []
    for cell in cells:
        reason = cost_undefined_reason(cell)
        if reason is not None:
            listed.append({"cell_id": cell["cell_id"], "reason": reason})
    return listed


def demand_domain_max(cells: list[dict], requested_max) -> tuple[Fraction, str | None]:
    """Limita o domínio ao maior `D` em que toda célula censurada ainda tem `n`
    determinado (`D ≤ L`). Extrapolar além do único limite medido seria o mesmo
    pecado de usar o teto como se fosse `S`, um passo adiante."""
    requested = _as_fraction(requested_max)
    bounds = [
        _as_fraction(c["saturation_lower_bound"])
        for c in cells
        if c.get("saturation_censored", False) and c.get("saturation_lower_bound") is not None
    ]
    if not bounds:
        return requested, None

    limit = min(bounds)
    if requested <= limit:
        return requested, None
    return limit, (
        f"domínio de demanda limitado a {float(limit):.0f} req/s: acima disso o número de "
        "unidades de células censuradas fica indeterminado (só se sabe que S ≥ esse valor)."
    )


def censorship_warning(cells: list[dict]) -> str | None:
    """Aviso quando mais de metade das células medidas ficou censurada.

    O sentido mudou junto com o modelo: censuradas colapsam todas em `n = 1` no
    domínio inteiro, então o custo para de discriminá-las entre si — sobram
    apenas a parcela de armazenamento por unidade e a latência. Não altera o
    cálculo de dominância (docs/DESIGN.md manda sinalizar, nunca substituir)."""
    if not cells:
        return None
    censored_count = sum(1 for c in cells if c.get("saturation_censored", False))
    if censored_count > len(cells) / 2:
        return (
            f"{censored_count}/{len(cells)} células ficaram censuradas na vazão de saturação "
            "(não violaram o SLO nem no teto da rampa curta) — todas precisam de exatamente 1 "
            "unidade no domínio medido, então o custo não as discrimina entre si; a comparação "
            "recai sobre armazenamento por unidade e latência."
        )
    return None
