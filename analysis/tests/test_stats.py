"""Dados sintéticos com propriedade conhecida — mesma disciplina de
harness/ aplicada à estatística (ver plano da Etapa 8:
implementacao-md-com-base-nos-staged-snowflake.md)."""

from __future__ import annotations

import numpy as np
import pytest

from analysis.stats import (
    bootstrap_percentile_ci,
    dunn_posthoc,
    effect_size_epsilon_squared,
    kruskal_wallis,
    tost_equivalence,
    vargha_delaney_a,
    vargha_delaney_magnitude,
)


def test_kruskal_wallis_does_not_reject_when_groups_share_a_distribution():
    rng = np.random.default_rng(1)
    groups = [rng.normal(10, 1, 200).tolist() for _ in range(3)]
    result = kruskal_wallis(groups)
    assert result.reject_h0 is False
    assert result.p_value > 0.05


def test_kruskal_wallis_rejects_when_one_group_is_shifted():
    rng = np.random.default_rng(2)
    a = rng.normal(10, 1, 200).tolist()
    b = rng.normal(10, 1, 200).tolist()
    c = rng.normal(30, 1, 200).tolist()  # deslocado bem acima das outras duas
    result = kruskal_wallis([a, b, c])
    assert result.reject_h0 is True
    assert result.p_value < 0.05


def test_dunn_posthoc_points_at_the_shifted_pair():
    rng = np.random.default_rng(3)
    groups = {
        "a": rng.normal(10, 1, 200).tolist(),
        "b": rng.normal(10, 1, 200).tolist(),
        "c": rng.normal(30, 1, 200).tolist(),
    }
    pairwise = dunn_posthoc(groups)
    assert pairwise[("a", "c")] < 0.05
    assert pairwise[("b", "c")] < 0.05
    assert pairwise[("a", "b")] > 0.05


def test_bootstrap_ci_covers_the_true_percentile_at_roughly_the_nominal_rate():
    true_median = 0.0
    hits = 0
    trials = 30
    for i in range(trials):
        rng = np.random.default_rng(1000 + i)
        sample = rng.normal(loc=true_median, scale=1.0, size=200).tolist()
        ci = bootstrap_percentile_ci(sample, percentile=0.5, n_resamples=500, seed=i)
        if ci.low <= true_median <= ci.high:
            hits += 1
    # nominal é 95%; com só 30 tentativas e n_resamples reduzido, exigimos
    # cobertura "aproximadamente nominal" em vez do valor exato.
    assert hits / trials >= 0.80


def test_effect_size_epsilon_squared_is_near_zero_for_identical_distributions():
    rng = np.random.default_rng(4)
    groups = [rng.normal(10, 1, 200).tolist() for _ in range(3)]
    result = kruskal_wallis(groups)
    epsilon_sq = effect_size_epsilon_squared(result.h_statistic, n_total=600, n_groups=3)
    assert abs(epsilon_sq) < 0.05


def test_effect_size_epsilon_squared_is_large_for_a_clearly_shifted_group():
    rng = np.random.default_rng(5)
    a = rng.normal(10, 1, 200).tolist()
    b = rng.normal(10, 1, 200).tolist()
    c = rng.normal(30, 1, 200).tolist()
    result = kruskal_wallis([a, b, c])
    epsilon_sq = effect_size_epsilon_squared(result.h_statistic, n_total=600, n_groups=3)
    assert epsilon_sq > 0.5


def test_tost_concludes_equivalence_for_samples_with_the_same_mean():
    rng = np.random.default_rng(6)
    a = rng.normal(100, 5, 300).tolist()
    b = rng.normal(100, 5, 300).tolist()
    result = tost_equivalence(a, b, low_bound=-5, high_bound=5)
    assert result.equivalent is True


def test_tost_does_not_conclude_equivalence_for_samples_separated_beyond_the_margin():
    rng = np.random.default_rng(7)
    a = rng.normal(100, 5, 300).tolist()
    b = rng.normal(130, 5, 300).tolist()  # diferença de 30, muito além da margem de ±5
    result = tost_equivalence(a, b, low_bound=-5, high_bound=5)
    assert result.equivalent is False


def test_vargha_delaney_a_is_near_half_for_identical_distributions():
    rng = np.random.default_rng(8)
    a = rng.normal(10, 1, 300).tolist()
    b = rng.normal(10, 1, 300).tolist()
    assert abs(vargha_delaney_a(a, b) - 0.5) < 0.05


def test_vargha_delaney_a_is_large_when_a_is_clearly_shifted_above_b():
    rng = np.random.default_rng(9)
    a = rng.normal(30, 1, 300).tolist()
    b = rng.normal(10, 1, 300).tolist()
    assert vargha_delaney_a(a, b) > 0.71  # limiar "grande" de Vargha & Delaney (2000)


def test_vargha_delaney_a_mirrors_below_half_when_arguments_swap():
    rng = np.random.default_rng(10)
    a = rng.normal(30, 1, 300).tolist()
    b = rng.normal(10, 1, 300).tolist()
    # A12(a, b) = 1 - A12(b, a) — mesma base de postos do Mann-Whitney U,
    # só espelhada pela troca de qual grupo é "referência".
    assert vargha_delaney_a(a, b) == pytest.approx(1 - vargha_delaney_a(b, a))


@pytest.mark.parametrize(
    ("a12", "expected"),
    [
        (0.50, "desprezivel"),
        (0.559, "desprezivel"),
        (0.56, "pequena"),
        (0.64, "media"),
        (0.71, "grande"),  # o próprio limiar — float faria 0.71-0.5 < 0.21
        (0.99, "grande"),
    ],
)
def test_vargha_delaney_magnitude_uses_the_published_thresholds(a12, expected):
    assert vargha_delaney_magnitude(a12) == expected


def test_vargha_delaney_magnitude_is_symmetric_around_half():
    # A12 = 0,01 (b quase sempre mais lenta) é tão "grande" quanto 0,99 — o
    # rótulo mede distância a 0,5, a direção fica no valor.
    for a12 in (0.52, 0.40, 0.30, 0.01):
        assert vargha_delaney_magnitude(a12) == vargha_delaney_magnitude(1 - a12)
