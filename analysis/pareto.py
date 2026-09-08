"""Dominância de Pareto em 2 dimensões — latência p99 × custo por milhão de
requisições.

A vazão de saturação NÃO é um terceiro eixo. Ela entra no DENOMINADOR do
custo: `S` é a capacidade máxima que uma unidade de atendimento sustenta por
mês, e é contra essa capacidade que o custo mensal da unidade é normalizado
(ver docs/DESIGN.md, "Custo como função da demanda").

Modelo (docs/DESIGN.md):

    n   = ⌈V_mem / M⌉                    unidades (piso de memória; 1 em disco)
    C_f = n · p_i · h                     [fluxo: computação, $/mês]
    C_a = n · V_disco · p_a               [estoque: armazenamento, $/mês]
    C   = (C_f + C_a) · 10^6 / (S · 2.592.000)   [$ por milhão de requisições]

Não existe mais uma demanda `D` externa a varrer: a análise sempre opera na
capacidade máxima de UMA unidade de atendimento. O `n` acima é só o piso de
memória (`⌈V_mem/M⌉`) — hoje sempre 1 nos dados reais, mas mantido para uma
extrapolação futura de `U` maior (H1) não ficar silenciosamente errada.

`C` é DECRESCENTE em `S`: quanto maior a vazão de saturação de uma célula,
menor o custo por requisição. Para células censuradas (só se conhece
`S ≥ L`), isso dá um TETO de custo (usando `L`), nunca um piso — inventar um
piso exigiria um teto de `S` que não foi medido; o piso fica em 0.

Aritmética em `fractions.Fraction` para a banda de tolerância: `S·(1±TOL)`
não é diádico (`1,2` não tem representação binária exata).
"""

from __future__ import annotations

import math
from fractions import Fraction

# docs/DESIGN.md: a rampa curta é ensaio único, sem estimativa de variância.
# A tolerância propaga a incerteza de `S` diretamente para o custo (que agora
# divide por S) — incide só sobre `S`, que é medido; nunca sobre o piso de
# capacidade de memória, que é exato.
TOLERANCE = 0.20

# Segundos em 30 dias — converte a vazão de saturação (req/s) em capacidade
# mensal (req/mês). Nota: HOURS_PER_MONTH (analysis/report.py) usa 730 h
# (30,42 dias-média); aqui usa-se 30 dias exatos. São convenções mensais
# padrão distintas e a discrepância de ~1,4% entre elas não muda nenhuma
# comparação (afeta todas as células igualmente) — declarar no texto do TCC.
SECONDS_PER_MONTH = 2_592_000


def _as_fraction(value) -> Fraction:
    """Converte pela intenção DECIMAL do valor, não pelo binário do float.

    `Fraction(0.2)` devolve 3602879701896397/18014398509481984 — o binário
    exato do float, que não é 1/5. `str()` usa a repr mais curta que faz
    round-trip, então `Fraction(str(0.2))` é exatamente 1/5."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, float):
        return Fraction(str(value))
    return Fraction(value)


def capacity_units(cell: dict) -> int:
    """`⌈V_mem / M⌉` — quantas unidades a base exige só para caber em memória.

    Vale 1 para mecanismos em disco (`memory_bytes` ausente ou zero) — hoje
    também vale 1 para os em memória, nos dados reais (nenhuma célula chega
    perto de `M`), mas o cálculo fica pronto para quando isso deixar de valer."""
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


def _saturation_point(cell: dict) -> Fraction | None:
    """Estimativa PONTUAL de `S` — só existe para células não censuradas.

    Censuradas só têm um piso (`saturation_lower_bound`): sob um custo que
    divide por `S`, um piso não dá um ponto, só um teto de custo (ver
    `cost_per_million_requests_bounds`)."""
    if cell.get("saturation_censored", False):
        return None
    saturation = cell.get("saturation_throughput_approx")
    if saturation is None or saturation <= 0:
        return None
    return _as_fraction(saturation)


def _saturation_conservative_lower(cell: dict) -> Fraction | None:
    """O menor `S` defensável: o ponto medido, ou o piso de uma censurada.
    Como o custo é decrescente em `S`, é este valor que produz o TETO de
    custo (nunca um piso — o piso exigiria um teto de `S`, que não existe)."""
    if cell.get("saturation_censored", False):
        lower_bound = cell.get("saturation_lower_bound")
        return _as_fraction(lower_bound) if lower_bound is not None else None
    return _saturation_point(cell)


def cost_per_million_requests(cell: dict) -> float | None:
    """Estimativa PONTUAL de custo por milhão de requisições — só definida
    quando `S` tem um ponto medido (célula não censurada). Censuradas nunca
    aparecem aqui: usar `cost_per_million_requests_bounds` para o teto que
    ainda é conhecido, ou `cost_undefined_reason`/`cells_without_cost` para
    saber por que não há ponto."""
    unit_cost = cell.get("unit_cost_usd_month")
    saturation = _saturation_point(cell)
    if unit_cost is None or saturation is None:
        return None
    units = capacity_units(cell)
    return float(units * unit_cost * 1_000_000 / (saturation * SECONDS_PER_MONTH))


def cost_per_million_requests_bounds(
    cell: dict, tolerance: float = TOLERANCE
) -> tuple[float, float] | None:
    """`(custo_lo, custo_hi)` — banda de `±tolerance` sobre `S`.

    `S` alto → custo baixo, então a banda inverte em relação à de `S`:
    `custo_lo` vem do extremo ALTO de `S`, `custo_hi` do extremo BAIXO.

    Censuradas: só o teto é conhecido (via `L`, o `lower_bound` medido); o
    piso fica em 0 — sem um teto medido de `S`, não há como calcular um piso
    de custo honesto, e inventar um deixaria a dominância mais permissiva do
    que os dados sustentam, não mais conservadora."""
    unit_cost = cell.get("unit_cost_usd_month")
    if unit_cost is None:
        return None
    units = capacity_units(cell)

    if cell.get("saturation_censored", False):
        lower = _saturation_conservative_lower(cell)
        if lower is None:
            return None
        cost_hi = float(units * unit_cost * 1_000_000 / (lower * SECONDS_PER_MONTH))
        return (0.0, cost_hi)

    saturation = _saturation_point(cell)
    if saturation is None:
        return None
    tolerance_fraction = _as_fraction(tolerance)
    s_lo = saturation * (Fraction(1) - tolerance_fraction)
    s_hi = saturation * (Fraction(1) + tolerance_fraction)
    cost_lo = float(units * unit_cost * 1_000_000 / (s_hi * SECONDS_PER_MONTH))
    cost_hi = float(units * unit_cost * 1_000_000 / (s_lo * SECONDS_PER_MONTH))
    return (cost_lo, cost_hi)


def dominates(a: dict, b: dict, tolerance: float = TOLERANCE) -> bool:
    """`a` domina `b`: melhor-ou-igual nas 2 dimensões e estritamente melhor
    em ao menos uma.

    Custo compara intervalos (só domina se forem disjuntos, absorvendo a
    incerteza de `S`); latência compara valores diretos, sem tolerância — ela
    vem de 5 repetições com IC de bootstrap, não de um ensaio único.

    Custo indefinido nunca domina nem é dominado: uma célula sem `S` não pode
    ser colocada no plano, e fingir que pode a poria na fronteira de graça."""
    bounds_a = cost_per_million_requests_bounds(a, tolerance)
    bounds_b = cost_per_million_requests_bounds(b, tolerance)
    if bounds_a is None or bounds_b is None:
        return False

    cost_a_better = bounds_a[1] < bounds_b[0]
    cost_b_better = bounds_b[1] < bounds_a[0]
    latency_a_better = a["latency_p99_ms"] < b["latency_p99_ms"]
    latency_b_better = b["latency_p99_ms"] < a["latency_p99_ms"]

    better_or_equal = not cost_b_better and not latency_b_better
    strictly_better = cost_a_better or latency_a_better
    return better_or_equal and strictly_better


def pareto_frontier(cells: list[dict], tolerance: float = TOLERANCE) -> list[dict]:
    """Células não dominadas. Células sem custo definido ficam de fora (ver
    `cells_without_cost`)."""
    priced = [c for c in cells if cost_per_million_requests_bounds(c, tolerance) is not None]
    return [
        cell
        for cell in priced
        if not any(dominates(other, cell, tolerance) for other in priced if other is not cell)
    ]


def cheapest_cells(cells: list[dict], rel_tol: float = 1e-9) -> list[str]:
    """IDs de TODAS as células empatadas no menor custo por milhão de
    requisições, não apenas uma.

    Devolver uma única célula obrigava a desempatar, e o desempate alfabético
    produzia afirmação falsa: com memória fora do preço por GiB, células
    Valkey de mesma estratégia têm custo por unidade idêntico ao centavo, e o
    relatório saía dizendo "e1-valkey é a mais barata" quando o correto é
    "empatam". Empate é informação; escondê-lo atrás de um `sorted()` é
    inventar um vencedor.

    Usa a ESTIMATIVA PONTUAL — censuradas não têm ponto, então nunca entram
    aqui. `rel_tol` absorve só ruído de ponto flutuante entre valores
    aritmeticamente iguais; não é margem de equivalência prática."""
    priced = [
        (cost, cell["cell_id"])
        for cost, cell in ((cost_per_million_requests(c), c) for c in cells)
        if cost is not None
    ]
    if not priced:
        return []
    minimum = min(cost for cost, _ in priced)
    threshold = abs(minimum) * rel_tol
    return sorted(cell_id for cost, cell_id in priced if cost - minimum <= threshold)


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


def censorship_warning(cells: list[dict]) -> str | None:
    """Aviso quando mais de metade das células medidas ficou censurada.

    O sentido depende do modelo novo: uma censurada não fica sem custo (tem
    um TETO, via `L`), mas fica sem PONTO — não entra em `cheapest_cells`, e
    só domina/é dominada dentro do que esse teto permite. Sinaliza quando
    isso afeta uma fração grande das células, para ausência de uma célula
    censurada numa comparação pontual não ser lida como remoção silenciosa."""
    if not cells:
        return None
    censored_count = sum(1 for c in cells if c.get("saturation_censored", False))
    if censored_count > len(cells) / 2:
        return (
            f"{censored_count}/{len(cells)} células ficaram censuradas na vazão de saturação "
            "(não violaram o SLO nem no teto da rampa curta) — para essas, o custo por milhão "
            "de requisições só tem um TETO conhecido (via o piso de S medido), nunca um valor "
            "pontual; elas não entram em cheapest_cells e só dominam/são dominadas dentro do "
            "que esse teto permite."
        )
    return None
