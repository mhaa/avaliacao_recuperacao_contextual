"""Kruskal-Wallis + Dunn (Bonferroni) + IC de percentil por bootstrap + TOST
— docs/DESIGN.md, "Estatística": "Kruskal-Wallis; se rejeitar H0, Dunn com
correção de Bonferroni (α = 5%). Intervalos de confiança dos percentis por
bootstrap com 10.000 reamostras. Reportar também a magnitude do efeito."

Não-paramétrico em toda parte: a distribuição de latência é assimetrica por
natureza (docs/DESIGN.md, "regra de ouro": nunca reportar latência média), então
testes que assumem normalidade (ANOVA, Tukey, Cohen's d) não se aplicam —
daqui Kruskal-Wallis/Dunn em vez de ANOVA/Tukey, e epsilon-quadrado (o
companion não-paramétrico do tamanho de efeito) em vez de Cohen's d.

`tost_equivalence` existe para a etapa de confirmação (docs/DESIGN.md,
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
    docs/DESIGN.md: Dunn só entra "se rejeitar H0")."""
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


_BOOTSTRAP_BATCH_TARGET_BYTES = 1_000_000_000  # ~1GB por lote de reamostras


def bootstrap_percentile_ci(
    data: list[float],
    percentile: float,
    n_resamples: int = 10_000,
    confidence: float = 0.95,
    seed: int | None = None,
    batch: int | None = None,
) -> BootstrapCI:
    """IC do percentil por reamostragem — docs/DESIGN.md: "Intervalos de
    confiança dos percentis por bootstrap com 10.000 reamostras"."""
    sample = np.asarray(data, dtype=float)

    def statistic(resampled, axis):
        return np.percentile(resampled, percentile * 100, axis=axis)

    if batch is None:
        # Sem `batch`, scipy vetoriza todas as `n_resamples` de uma vez —
        # aloca uma matriz (n_resamples, len(sample)): com 10.000 reamostras
        # sobre uma célula real (~1.5M linhas de medição), isso é ~120 GB e
        # explode a memória (confirmado ao vivo: container morto com OOM,
        # exit 137). `batch` limita quantas reamostras ficam na matriz por
        # vez, sem mudar o resultado — só o pico de memória.
        #
        # Adaptativo em vez de um número fixo: a confirmação pool TODAS as
        # combinações de carga/seletividade por célula (load_cell_latencies),
        # chegando a 20-30M+ linhas por célula — ~20x o tamanho de célula
        # (~1.5M) para o qual `batch=50` fixo foi dimensionado. Um `batch`
        # fixo nesse volume estoura o alvo de memória por lote; um `batch`
        # pequeno demais em amostras pequenas desperdiça paralelismo do
        # scipy à toa. Mirar um teto de memória por lote (não um número de
        # reamostras) escala com o tamanho real da amostra nos dois
        # sentidos — confirmado necessário rodando build_report em paralelo
        # por célula (ProcessPoolExecutor): vários workers, cada um com seu
        # próprio lote, precisam caber juntos na mesma VM.
        batch = max(1, min(n_resamples, _BOOTSTRAP_BATCH_TARGET_BYTES // (8 * max(len(sample), 1))))

    result = scipy_stats.bootstrap(
        (sample,),
        statistic,
        n_resamples=n_resamples,
        confidence_level=confidence,
        method="percentile",
        random_state=np.random.default_rng(seed),
        batch=batch,
    )
    return BootstrapCI(
        low=float(result.confidence_interval.low), high=float(result.confidence_interval.high)
    )


def effect_size_epsilon_squared(h_statistic: float, n_total: int, n_groups: int) -> float:
    """epsilon-quadrado (Tomczak & Tomczak, 2014) — companion não-paramétrico
    do Kruskal-Wallis, análogo ao R² de uma ANOVA; Cohen's d não se aplica
    porque assume normalidade (docs/DESIGN.md: "Reportar também a magnitude do
    efeito")."""
    return (h_statistic - n_groups + 1) / (n_total - n_groups)


def vargha_delaney_a(a: list[float], b: list[float]) -> float:
    """A de Vargha-Delaney (A12, Vargha & Delaney 2000) — probabilidade de uma
    observação aleatória de `a` ser maior que uma de `b` (empate conta meio).
    Complementa Dunn: Dunn só diz "esse par difere?" (e, em amostras grandes
    como as deste projeto — centenas de milhares de requisições por célula —
    quase sempre "sim", tornando o p-valor pouco informativo sozinho); A12 diz
    o QUANTO, numa escala 0-1 com limiares convencionados (Vargha & Delaney,
    2000): ~0,56 pequeno, ~0,64 médio, ~0,71 grande (e o espelho abaixo de 0,5
    para a direção oposta — A12(a,b) = 1 - A12(b,a)). Ao contrário de Dunn,
    não é um teste de hipótese com taxa de falso-positivo a proteger, então
    não fica atrás do gate `reject_h0` do Kruskal-Wallis.

    Calculado via U de Mann-Whitney (mesma base de postos do Dunn, sem exigir
    normalidade): A12 = U_a / (n_a * n_b), onde U_a conta pares (a_i, b_j) com
    a_i > b_j (empate soma 0,5) — a fórmula por contagem direta é O(n_a*n_b),
    inviável nos tamanhos de amostra reais do projeto."""
    a_arr = np.asarray(a, dtype=float)
    b_arr = np.asarray(b, dtype=float)
    u_a = scipy_stats.mannwhitneyu(a_arr, b_arr, alternative="two-sided").statistic
    return float(u_a / (len(a_arr) * len(b_arr)))


def vargha_delaney_magnitude(a12: float) -> str:
    """Rótulo de magnitude de um A12 pelos limiares de Vargha & Delaney
    (2000) — 0,56 / 0,64 / 0,71 e os espelhos abaixo de 0,5 —, sobre a
    distância a 0,5, então vale nas duas direções: A12 = 0,01 é "grande"
    tanto quanto 0,99. A direção (qual lado é mais lento) fica no próprio
    valor; este rótulo só diz o quanto."""
    # round: 0.71 - 0.5 == 0.20999999999999996 em float, o que jogaria o
    # próprio limiar de "grande" para "media".
    distance = round(abs(a12 - 0.5), 10)
    if distance < 0.06:
        return "desprezivel"
    if distance < 0.14:
        return "pequena"
    if distance < 0.21:
        return "media"
    return "grande"


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
