"""Kruskal-Wallis + Dunn (Bonferroni) + IC de percentil por bootstrap + TOST
— CONTEXTO.md, "Estatística": "Kruskal-Wallis; se rejeitar H0, Dunn com
correção de Bonferroni (α = 5%). Intervalos de confiança dos percentis por
bootstrap com 10.000 reamostras. Reportar também a magnitude do efeito."

Não-paramétrico em toda parte: a distribuição de latência é assimetrica por
natureza (CONTEXTO.md, "regra de ouro": nunca reportar latência média), então
testes que assumem normalidade (ANOVA, Tukey, Cohen's d) não se aplicam —
daqui Kruskal-Wallis/Dunn em vez de ANOVA/Tukey, e epsilon-quadrado (o
companion não-paramétrico do tamanho de efeito) em vez de Cohen's d.

`tost_equivalence` existe para a etapa de confirmação (CONTEXTO.md,
"Delineamento em duas etapas") alegar equivalência prática entre células da
fronteira de Pareto — "não rejeitou H0" não é o mesmo que "são equivalentes".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scikit_posthocs as sp
from scipy import stats as scipy_stats


@dataclass(frozen=True)
class KruskalResult:
    h_statistic: float
    p_value: float
    reject_h0: bool


def kruskal_wallis(groups: list[list[float]], alpha: float = 0.05) -> KruskalResult:
    h_statistic, p_value = scipy_stats.kruskal(*groups)
    return KruskalResult(
        h_statistic=float(h_statistic), p_value=float(p_value), reject_h0=bool(p_value < alpha)
    )


def dunn_posthoc(groups: dict[str, list[float]]) -> dict[tuple[str, str], float]:
    """Matriz de p-valores pareados (Bonferroni), achatada em um dict —
    só faz sentido chamar depois de `kruskal_wallis(...).reject_h0` (
    CONTEXTO.md: Dunn só entra "se rejeitar H0")."""
    labels = list(groups)
    data = [np.asarray(groups[label], dtype=float) for label in labels]
    p_matrix = sp.posthoc_dunn(data, p_adjust="bonferroni").to_numpy()
    return {
        (labels[i], labels[j]): float(p_matrix[i, j])
        for i in range(len(labels))
        for j in range(len(labels))
        if i != j
    }


@dataclass(frozen=True)
class BootstrapCI:
    low: float
    high: float


def bootstrap_percentile_ci(
    data: list[float],
    percentile: float,
    n_resamples: int = 10_000,
    confidence: float = 0.95,
    seed: int | None = None,
) -> BootstrapCI:
    """IC do percentil por reamostragem — CONTEXTO.md: "Intervalos de
    confiança dos percentis por bootstrap com 10.000 reamostras"."""
    sample = np.asarray(data, dtype=float)

    def statistic(resampled, axis):
        return np.percentile(resampled, percentile * 100, axis=axis)

    result = scipy_stats.bootstrap(
        (sample,),
        statistic,
        n_resamples=n_resamples,
        confidence_level=confidence,
        method="percentile",
        random_state=np.random.default_rng(seed),
    )
    return BootstrapCI(
        low=float(result.confidence_interval.low), high=float(result.confidence_interval.high)
    )


def effect_size_epsilon_squared(h_statistic: float, n_total: int, n_groups: int) -> float:
    """epsilon-quadrado (Tomczak & Tomczak, 2014) — companion não-paramétrico
    do Kruskal-Wallis, análogo ao R² de uma ANOVA; Cohen's d não se aplica
    porque assume normalidade (CONTEXTO.md: "Reportar também a magnitude do
    efeito")."""
    return (h_statistic - n_groups + 1) / (n_total - n_groups)


@dataclass(frozen=True)
class TostResult:
    equivalent: bool
    p_greater: float
    p_less: float


def tost_equivalence(
    a: list[float], b: list[float], low_bound: float, high_bound: float, alpha: float = 0.05
) -> TostResult:
    """Dois testes t unilaterais (TOST): equivalente só se as DUAS hipóteses
    nulas (diferença <= low_bound; diferença >= high_bound) forem rejeitadas.
    Implementado deslocando `b` por cada margem, em vez de testar a
    diferença bruta contra 0 — forma padrão de reduzir TOST a dois
    `ttest_ind` de uma cauda."""
    a_arr = np.asarray(a, dtype=float)
    b_arr = np.asarray(b, dtype=float)
    p_greater = scipy_stats.ttest_ind(a_arr, b_arr + low_bound, alternative="greater").pvalue
    p_less = scipy_stats.ttest_ind(a_arr, b_arr + high_bound, alternative="less").pvalue
    equivalent = bool(p_greater < alpha and p_less < alpha)
    return TostResult(equivalent=equivalent, p_greater=float(p_greater), p_less=float(p_less))
