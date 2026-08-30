"""Dominância de Pareto em 3 dimensões — latência p99, custo, vazão de
saturação (CONTEXTO.md, "Delineamento em duas etapas"). A vazão vem da
rampa curta (load/saturation.py), de ensaio único — sem estimativa de
variância, por isso a tolerância de 20% na comparação (latência e custo já
vêm de 5 repetições, com IC de bootstrap, sem necessidade de tolerância
própria aqui).

Uma célula é dominada se outra for melhor-ou-igual nas 3 dimensões e
estritamente melhor em pelo menos uma. Células cuja rampa curta não
saturou nem no teto (`saturation_censored=True`) formam sua própria classe
de equivalência no topo da dimensão de vazão: empatadas entre si, e
superiores a qualquer célula não censurada — nunca o valor do teto é usado
como se fosse a vazão real medida.
"""

from __future__ import annotations

TOLERANCE = 0.20  # CONTEXTO.md: rampa curta é ensaio único, sem variância


def throughput_dominates(a: dict, b: dict) -> bool:
    """`a` domina `b` na dimensão de vazão isoladamente. Regra: ambas
    censuradas -> empate (não domina); só `a` censurada -> domina; só `b`
    censurada -> não domina; nem uma censurada -> domina se a diferença
    relativa passar da tolerância de 20%."""
    a_censored = bool(a.get("saturation_censored", False))
    b_censored = bool(b.get("saturation_censored", False))

    if a_censored and b_censored:
        return False
    if a_censored:
        return True
    if b_censored:
        return False

    a_value = a["saturation_throughput_approx"]
    b_value = b["saturation_throughput_approx"]
    if a_value is None or b_value is None:
        return False
    if b_value == 0:
        return a_value > b_value
    return (a_value - b_value) / b_value > TOLERANCE


def dominates(a: dict, b: dict) -> bool:
    """`a` domina `b`: melhor-ou-igual em latência (menor), custo (menor)
    e vazão (`throughput_dominates`), estritamente melhor em ao menos uma."""
    better_or_equal = True
    strictly_better = False

    if a["latency_p99_ms"] > b["latency_p99_ms"]:
        better_or_equal = False
    elif a["latency_p99_ms"] < b["latency_p99_ms"]:
        strictly_better = True

    if a["cost_usd_hour"] > b["cost_usd_hour"]:
        better_or_equal = False
    elif a["cost_usd_hour"] < b["cost_usd_hour"]:
        strictly_better = True

    if throughput_dominates(b, a):
        better_or_equal = False
    elif throughput_dominates(a, b):
        strictly_better = True

    return better_or_equal and strictly_better


def pareto_frontier(cells: list[dict]) -> list[dict]:
    """Retorna só as células não dominadas por nenhuma outra."""
    return [
        cell
        for cell in cells
        if not any(dominates(other, cell) for other in cells if other is not cell)
    ]


def censorship_warning(cells: list[dict]) -> str | None:
    """Se mais de metade das células viáveis medidas ficaram censuradas na
    dimensão de vazão, retorna uma mensagem de aviso — não altera o
    cálculo de dominância (CONTEXTO.md: "sinalizar", nunca substituir
    automaticamente); `None` caso contrário."""
    if not cells:
        return None
    censored_count = sum(1 for c in cells if c.get("saturation_censored", False))
    if censored_count > len(cells) / 2:
        return (
            f"{censored_count}/{len(cells)} células ficaram censuradas na dimensão de "
            "vazão de saturação (não violaram o SLO nem no teto da rampa curta) — a "
            "dimensão não discriminou as configurações neste delineamento; considere "
            "reduzir a fronteira de Pareto a latência × custo."
        )
    return None
