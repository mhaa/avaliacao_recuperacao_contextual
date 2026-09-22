"""Testes de load/ramp.py — cronograma da rampa de estresse e veredito de
recuperação. Tudo em memória, sem rede/gcloud/k6, mesma disciplina de
load/tests/test_saturation.py."""

from __future__ import annotations

import pytest

from load.ramp import (
    DEFAULT_RECOVERY_TOLERANCE,
    RampStep,
    RampStepResult,
    build_step_schedule,
    classify_recovery,
    estimated_request_count,
    generator_saturated,
)


def _phases(schedule: list[RampStep]) -> list[str]:
    return [s.phase for s in schedule]


def _rates(schedule: list[RampStep], phase: str) -> list[int]:
    return [s.rate for s in schedule if s.phase == phase]


def test_schedule_runs_coarse_then_fine_then_plateau_then_down():
    schedule = build_step_schedule(knee=10_000, fine_step=1_000)
    phases = _phases(schedule)

    # As quatro fases aparecem, nesta ordem, sem intercalar.
    assert phases == sorted(
        phases, key=lambda p: ["coarse_up", "fine_up", "plateau", "fine_down"].index(p)
    )
    assert set(phases) == {"coarse_up", "fine_up", "plateau", "fine_down"}


def test_coarse_phase_stops_before_the_fine_phase_starts():
    schedule = build_step_schedule(knee=10_000, fine_step=1_000)
    coarse, fine = _rates(schedule, "coarse_up"), _rates(schedule, "fine_up")

    # fine_start_fraction=0.6 de 10.000 = 6.000: a fase grossa vai de 1.000 a
    # 5.000 e a fina assume em 6.000, sem repetir taxa nem deixar buraco.
    assert coarse == [1000, 2000, 3000, 4000, 5000]
    assert fine[0] == 6000


def test_ascent_overshoots_the_projected_knee():
    # Atravessar o joelho é o objetivo: um cronograma que parasse nele nunca
    # observaria a sobrecarga, que é o fenômeno sob estudo.
    schedule = build_step_schedule(knee=10_000, fine_step=1_000)
    assert max(_rates(schedule, "fine_up")) == 13_000  # overshoot de 30%


def test_descent_mirrors_the_fine_ascent_rates():
    # Sem taxas coincidentes não há par para comparar, e a histerese fica
    # imensurável — é o que torna esta simetria um requisito, não um detalhe.
    schedule = build_step_schedule(knee=10_000, fine_step=1_000)
    up, down = _rates(schedule, "fine_up"), _rates(schedule, "fine_down")

    assert down == list(reversed(up[:-1]))
    assert down == sorted(down, reverse=True)


def test_plateau_sits_at_the_top_of_the_ascent_and_lasts_longer():
    schedule = build_step_schedule(knee=10_000, fine_step=1_000)
    plateau = [s for s in schedule if s.phase == "plateau"]
    fine_up = [s for s in schedule if s.phase == "fine_up"]

    assert len(plateau) == 1
    assert plateau[0].rate == max(s.rate for s in fine_up)
    assert plateau[0].duration_s > fine_up[0].duration_s


def test_schedule_never_exceeds_the_ceiling():
    schedule = build_step_schedule(knee=45_000, fine_step=1_000, ceiling=50_000)
    assert max(s.rate for s in schedule) == 50_000


def test_fine_step_that_misses_the_top_still_offers_it():
    # 6.000 + 700k nunca cai exatamente em 13.000; o topo precisa ser
    # oferecido mesmo assim, ou o patamar de sobrecarga ficaria abaixo do
    # alvo projetado.
    schedule = build_step_schedule(knee=10_000, fine_step=700)
    assert max(_rates(schedule, "fine_up")) == 13_000


@pytest.mark.parametrize(
    "kwargs",
    [
        {"knee": 0, "fine_step": 100},
        {"knee": -1, "fine_step": 100},
        {"knee": 10_000, "fine_step": 0},
        {"knee": 10_000, "fine_step": 100, "fine_start_fraction": 0.0},
        {"knee": 10_000, "fine_step": 100, "fine_start_fraction": 1.0},
    ],
)
def test_invalid_schedule_parameters_fail_loudly(kwargs):
    with pytest.raises(ValueError):
        build_step_schedule(**kwargs)


def test_estimated_request_count_is_the_sum_of_rate_times_duration():
    schedule = [
        RampStep(1000, "coarse_up", 30),
        RampStep(2000, "fine_up", 60),
    ]
    assert estimated_request_count(schedule) == 1000 * 30 + 2000 * 60


def _healthy(rate: int, phase: str, p99: float, throughput: float | None = None):
    return RampStepResult(
        rate=rate,
        phase=phase,
        throughput_rps=throughput if throughput is not None else float(rate),
        latency_ms_p50=2.0,
        latency_ms_p95=3.0,
        latency_ms_p99=p99,
        offered_load_ok=True,
        violated_slo=False,
    )


def test_recovery_verdict_is_recovered_when_the_descent_matches_the_ascent():
    results = [
        _healthy(4000, "fine_up", p99=100.0),
        _healthy(4000, "fine_down", p99=105.0),
    ]
    verdict = classify_recovery(results)

    assert verdict.verdict == "recovered"
    assert all(c.within_tolerance for c in verdict.comparisons)


def test_recovery_verdict_is_lost_on_the_real_e4_valkey_signature():
    # Dados reais de results/e4-valkey/.../saturation_high.json: 4.695 req/s
    # eram limpos na subida (p99 131 ms), mas ao voltar para ~4.7k req/s o
    # p99 ficou em 3.987 ms — o banco não se recuperou sozinho.
    results = [
        _healthy(4695, "fine_up", p99=131.0),
        RampStepResult(
            rate=4695,
            phase="fine_down",
            throughput_rps=4695.0,
            latency_ms_p50=2.0,
            latency_ms_p95=3.0,
            latency_ms_p99=3987.0,
            offered_load_ok=True,
            violated_slo=True,
        ),
    ]
    verdict = classify_recovery(results)

    assert verdict.verdict == "lost"
    degraded = [c for c in verdict.comparisons if not c.within_tolerance]
    assert [c.metric for c in degraded] == ["latency_ms_p99"]


def test_recovery_catches_a_stuck_tail_even_when_throughput_came_back():
    # O desfecho que um p99 agregado sozinho confundiria: a vazão volta ao
    # normal, mas a cauda fica presa. Por isso o veredito é por percentil.
    results = [
        _healthy(4000, "fine_up", p99=100.0, throughput=4000.0),
        RampStepResult(
            rate=4000,
            phase="fine_down",
            throughput_rps=4000.0,
            latency_ms_p50=2.0,
            latency_ms_p95=3.0,
            latency_ms_p99=900.0,
            offered_load_ok=True,
            violated_slo=False,
        ),
    ]
    verdict = classify_recovery(results)

    assert verdict.verdict == "lost"
    throughput_pairs = [c for c in verdict.comparisons if c.metric == "throughput_rps"]
    assert all(c.within_tolerance for c in throughput_pairs)


def test_recovery_ignores_pairs_whose_ascent_step_was_already_degraded():
    # Comparar contra um degrau de subida que já violava não diz nada sobre
    # recuperação: os dois lados estariam ruins.
    results = [
        RampStepResult(
            rate=9000,
            phase="fine_up",
            throughput_rps=7000.0,
            latency_ms_p99=800.0,
            offered_load_ok=False,
            violated_slo=True,
        ),
        RampStepResult(
            rate=9000,
            phase="fine_down",
            throughput_rps=6000.0,
            latency_ms_p99=2000.0,
            offered_load_ok=False,
            violated_slo=True,
        ),
    ]
    verdict = classify_recovery(results)

    assert verdict.verdict == "undetermined"
    assert verdict.comparisons == []
    assert verdict.reason is not None


def test_recovery_is_undetermined_without_any_matching_rate():
    results = [_healthy(4000, "fine_up", p99=100.0), _healthy(3500, "fine_down", p99=100.0)]
    assert classify_recovery(results).verdict == "undetermined"


def test_recovery_tolerance_boundary_is_not_a_regression():
    # Exatamente na tolerância ainda conta como recuperado; o veredito só
    # vira "lost" ao ultrapassá-la.
    at_limit = 100.0 * (1 + DEFAULT_RECOVERY_TOLERANCE)
    results = [
        _healthy(4000, "fine_up", p99=100.0),
        _healthy(4000, "fine_down", p99=at_limit),
    ]
    assert classify_recovery(results).verdict == "recovered"


def test_measured_zero_generator_cpu_is_not_saturation():
    # Mesma distinção de load/saturation.py: 0.0 é "medido e ocioso"; None é
    # "não foi possível medir". Nenhum dos dois é gargalo do gerador.
    idle = RampStepResult(rate=1, phase="fine_up", generator_cpu_percent=0.0)
    unmeasured = RampStepResult(rate=1, phase="fine_up", generator_cpu_percent=None)
    saturated = RampStepResult(rate=1, phase="fine_up", generator_cpu_percent=75.0)

    assert generator_saturated(idle) is False
    assert generator_saturated(unmeasured) is False
    assert generator_saturated(saturated) is True
