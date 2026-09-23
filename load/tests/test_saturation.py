"""Testes de load/saturation.py — algoritmo de busca de vazão de
saturação. Sempre com um `probe_fn` falso (nunca rede/gcloud real), mesma
disciplina de storage/tests/fakes.py."""

from __future__ import annotations

import pytest

from load.saturation import (
    BACKWARD_WALK_MAX_STEPS,
    GENERATOR_CPU_THRESHOLD,
    ProbeResult,
    doubling_sequence,
    fine_sequence,
    run_linear_probe_sweep,
    run_saturation_search,
)


def test_doubling_sequence_doubles_and_truncates_at_the_ceiling():
    assert list(doubling_sequence(1000, 50_000)) == [1000, 2000, 4000, 8000, 16000, 32000, 50000]


def test_fine_sequence_increases_by_roughly_10_percent_and_ends_at_ceiling():
    seq = list(fine_sequence(1000, 2000))
    assert seq[0] == 1000
    assert seq[-1] == 2000
    assert all(b > a for a, b in zip(seq, seq[1:]))
    assert all(v < 2000 for v in seq[:-1])


def _healthy_probe(true_threshold: int):
    def probe_fn(rate: int) -> ProbeResult:
        return ProbeResult(rate=rate, violated_slo=rate > true_threshold, generator_cpu_percent=30.0)

    return probe_fn


def test_run_saturation_search_binary_search_converges_to_the_true_threshold():
    # doubling a partir de 1000: 1000,2000,4000,8000 (ok), 16000 (viola) —
    # busca binária entre 8000 e 16000, 3 iterações: 12000 (viola) ->
    # 10000 (ok) -> 11000 (ok) -> converge exatamente no limiar real.
    result = run_saturation_search(_healthy_probe(true_threshold=11_000), start_rate=1_000)
    assert result.approx_throughput == 11_000.0
    assert result.censored is False
    assert result.loadgen_bottleneck is False
    assert result.lower_bound is None


def test_run_saturation_search_never_violating_is_censored_at_the_ceiling():
    def never_violates(rate: int) -> ProbeResult:
        return ProbeResult(rate=rate, violated_slo=False, generator_cpu_percent=10.0)

    result = run_saturation_search(never_violates, start_rate=1_000, ceiling=50_000)
    assert result.censored is True
    assert result.approx_throughput is None
    assert result.lower_bound == 50_000.0
    assert result.loadgen_bottleneck is False


def test_run_saturation_search_stops_immediately_when_generator_saturates():
    calls: list[int] = []

    def probe_fn(rate: int) -> ProbeResult:
        calls.append(rate)
        cpu = GENERATOR_CPU_THRESHOLD + 1 if len(calls) == 2 else 30.0
        return ProbeResult(rate=rate, violated_slo=False, generator_cpu_percent=cpu)

    result = run_saturation_search(probe_fn, start_rate=1_000)

    assert result.loadgen_bottleneck is True
    assert result.approx_throughput is None
    assert result.censored is False
    assert len(calls) == 2  # nunca sonda um 3º patamar depois do gerador saturar


def test_unmeasured_generator_cpu_does_not_abort_the_search_and_is_flagged():
    # Falha de telemetria do Cloud Monitoring (None) não pode ser fatal —
    # abortar por atraso de ingestão já foi bug confirmado ao vivo — mas
    # também não pode passar calada como se fosse 0% (gerador ocioso).
    calls: list[int] = []

    def probe_fn(rate: int) -> ProbeResult:
        calls.append(rate)
        cpu = None if len(calls) == 2 else 30.0
        return ProbeResult(rate=rate, violated_slo=rate > 11_000, generator_cpu_percent=cpu)

    result = run_saturation_search(probe_fn, start_rate=1_000)

    assert result.loadgen_bottleneck is False  # não medido != gargalo
    assert result.generator_cpu_unmeasured is True  # mas fica visível
    assert result.approx_throughput == 11_000.0  # a busca foi até o fim
    assert len(calls) > 2


def test_final_level_is_reprobed_the_requested_number_of_times():
    """O S reportado deixa de ser ensaio único: o patamar aprovado é
    re-sondado, e essas repetições ficam separadas da trilha de busca."""
    calls: list[int] = []

    def probe(rate: int) -> ProbeResult:
        calls.append(rate)
        return ProbeResult(rate=rate, violated_slo=rate > 1200, generator_cpu_percent=10.0)

    result = run_saturation_search(
        probe, start_rate=1000, ceiling=5000, step_mode="fine", step=0.25, confirm_repetitions=5
    )

    assert result.approx_throughput is not None
    approx = int(result.approx_throughput)
    assert len(result.final_level_probes) == 5
    assert [p.rate for p in result.final_level_probes] == [approx] * 5
    # As repetições entram também na trilha completa, e o valor aproximado
    # NÃO é recalculado a partir delas.
    assert calls[-5:] == [approx] * 5


def test_a_censored_cell_has_no_final_level_to_confirm():
    """Sem patamar de violação não há o que repetir — e o custo de uma célula
    censurada nem usa S como valor pontual (⌈D/S⌉ = 1 sai da desigualdade)."""

    def never_violates(rate: int) -> ProbeResult:
        return ProbeResult(rate=rate, violated_slo=False, generator_cpu_percent=10.0)

    result = run_saturation_search(
        never_violates,
        start_rate=1000,
        ceiling=2000,
        step_mode="fine",
        step=0.25,
        confirm_repetitions=5,
    )

    assert result.censored is True
    assert result.final_level_probes == []


def test_measured_zero_cpu_is_not_reported_as_unmeasured():
    # 0.0 é uma LEITURA (gerador ocioso); só None é ausência de leitura.
    def idle_generator(rate: int) -> ProbeResult:
        return ProbeResult(rate=rate, violated_slo=rate > 4_000, generator_cpu_percent=0.0)

    result = run_saturation_search(idle_generator, start_rate=1_000)

    assert result.generator_cpu_unmeasured is False
    assert result.loadgen_bottleneck is False


def test_run_saturation_search_with_min_step_converges_more_precisely_than_the_default_iteration_cap():
    # Limiar real (11234) não cai num ponto médio exato dentro do teto
    # padrão de 5 iterações — a busca por contagem fixa converge só até
    # ~250 req/s do limiar real (mesma mecânica do teste acima, threshold
    # diferente). Com min_step=50 e um teto de iterações generoso o
    # bastante pra chegar lá, a busca continua até a largura do intervalo
    # caber no alvo, não até esgotar um número fixo de sondagens.
    result = run_saturation_search(
        _healthy_probe(true_threshold=11_234),
        start_rate=1_000,
        binary_search_min_step=50,
        binary_search_iterations=20,
    )
    assert result.approx_throughput is not None
    assert abs(result.approx_throughput - 11_234) <= 50


def test_run_saturation_search_min_step_still_respects_the_iteration_ceiling():
    # min_step pequeno demais pro teto de iterações não trava a busca pra
    # sempre — o teto de segurança continua valendo mesmo nesse modo.
    calls: list[int] = []

    def probe_fn(rate: int) -> ProbeResult:
        calls.append(rate)
        return ProbeResult(rate=rate, violated_slo=rate > 11_234, generator_cpu_percent=10.0)

    run_saturation_search(
        probe_fn, start_rate=1_000, binary_search_min_step=1, binary_search_iterations=3
    )
    # 1000,2000,4000,8000 (ok),16000 (viola) = 5 sondagens da rampa + no
    # máximo 3 da busca binária (teto de iterations) = no máximo 8.
    assert len(calls) <= 8


def test_run_saturation_search_steps_backward_before_binary_search_when_the_first_probe_already_violates():
    # Ponto de partida (ex.: vindo de uma triagem otimista, método pooled
    # antigo) já viola o SLO na própria primeira sondagem. Sem recuar,
    # last_valid fica no sentinela 0 e a busca binária receberia [0, 4000]
    # inteiro — sondaria rate=2000 (bem abaixo do limiar real) só para
    # descobrir o óbvio. Com o recuo em passos de 10%, nenhuma sondagem
    # deveria cair muito abaixo do limiar real (3600).
    calls: list[int] = []

    def probe_fn(rate: int) -> ProbeResult:
        calls.append(rate)
        return ProbeResult(rate=rate, violated_slo=rate > 3_600, generator_cpu_percent=10.0)

    result = run_saturation_search(
        probe_fn,
        start_rate=4_000,
        step_mode="fine",
        binary_search_min_step=50,
        binary_search_iterations=20,
    )
    assert abs(result.approx_throughput - 3_600) <= 50
    assert min(calls) > 3_000


def test_run_saturation_search_does_not_step_backward_without_min_step():
    # Sem min_step, o número de iterações da busca binária já é fixo
    # (binary_search_iterations) independente da largura do bracket —
    # recuar não economizaria nada, só somaria sondagens extras. O
    # comportamento antigo (bracket [0, start] inteiro) continua valendo.
    calls: list[int] = []

    def probe_fn(rate: int) -> ProbeResult:
        calls.append(rate)
        return ProbeResult(rate=rate, violated_slo=rate > 3_600, generator_cpu_percent=10.0)

    run_saturation_search(probe_fn, start_rate=4_000, step_mode="fine")
    # bracket [0, 4000]: o primeiro ponto médio da busca binária é 2000.
    assert 2_000 in calls


def test_run_saturation_search_backward_walk_gives_up_after_max_steps_for_a_pathological_cell():
    # Célula que viola até perto de zero (cenário raro/degenerado) — o
    # recuo não pode rodar pra sempre; desiste após BACKWARD_WALK_MAX_STEPS
    # e cai no bracket [0, high] como antes de existir o recuo.
    calls: list[int] = []

    def always_violates(rate: int) -> ProbeResult:
        calls.append(rate)
        return ProbeResult(rate=rate, violated_slo=True, generator_cpu_percent=10.0)

    run_saturation_search(
        always_violates,
        start_rate=4_000,
        step_mode="fine",
        binary_search_min_step=50,
        binary_search_iterations=5,
    )
    # 1 sondagem da rampa + no máximo BACKWARD_WALK_MAX_STEPS de recuo +
    # no máximo binary_search_iterations da busca binária = teto pequeno,
    # nunca explode mesmo numa célula degenerada.
    assert len(calls) <= 1 + BACKWARD_WALK_MAX_STEPS + 5


def test_run_saturation_search_without_min_step_keeps_the_old_fixed_iteration_behavior():
    # min_step=None (default) preserva byte-a-byte o comportamento antigo —
    # mesmo resultado do teste histórico
    # test_run_saturation_search_binary_search_converges_to_the_true_threshold,
    # chamado agora passando min_step explicitamente como None.
    result = run_saturation_search(
        _healthy_probe(true_threshold=11_000), start_rate=1_000, binary_search_min_step=None
    )
    assert result.approx_throughput == 11_000.0


def test_run_linear_probe_sweep_stops_at_the_first_violation():
    calls: list[int] = []

    def probe_fn(rate: int) -> ProbeResult:
        calls.append(rate)
        return ProbeResult(rate=rate, violated_slo=rate >= 4_000, generator_cpu_percent=10.0)

    result = run_linear_probe_sweep(probe_fn, start_rate=1_000, step=1_000, ceiling=50_000)

    assert calls == [1_000, 2_000, 3_000, 4_000]
    assert result.probes[-1].rate == 4_000
    assert result.probes[-1].violated_slo is True
    assert result.censored is False
    assert result.loadgen_bottleneck is False


def test_run_linear_probe_sweep_stops_when_generator_saturates():
    calls: list[int] = []

    def probe_fn(rate: int) -> ProbeResult:
        calls.append(rate)
        cpu = GENERATOR_CPU_THRESHOLD + 1 if len(calls) == 2 else 30.0
        return ProbeResult(rate=rate, violated_slo=False, generator_cpu_percent=cpu)

    result = run_linear_probe_sweep(probe_fn, start_rate=1_000, step=1_000, ceiling=50_000)

    assert result.loadgen_bottleneck is True
    assert result.censored is False
    assert len(calls) == 2  # nunca sonda um 3º patamar depois do gerador saturar


def test_run_linear_probe_sweep_reaches_the_ceiling_cleanly():
    def never_violates(rate: int) -> ProbeResult:
        return ProbeResult(rate=rate, violated_slo=False, generator_cpu_percent=10.0)

    result = run_linear_probe_sweep(never_violates, start_rate=1_000, step=1_000, ceiling=3_000)

    assert result.censored is True
    assert result.loadgen_bottleneck is False
    assert [p.rate for p in result.probes] == [1_000, 2_000, 3_000]


def test_run_linear_probe_sweep_stops_on_the_first_probe_when_it_already_violates():
    calls: list[int] = []

    def probe_fn(rate: int) -> ProbeResult:
        calls.append(rate)
        return ProbeResult(rate=rate, violated_slo=True, generator_cpu_percent=10.0)

    result = run_linear_probe_sweep(probe_fn, start_rate=1_000, step=1_000, ceiling=50_000)

    assert calls == [1_000]
    assert result.probes[0].violated_slo is True


def test_run_linear_probe_sweep_respects_a_custom_step():
    calls: list[int] = []

    def never_violates(rate: int) -> ProbeResult:
        calls.append(rate)
        return ProbeResult(rate=rate, violated_slo=False, generator_cpu_percent=10.0)

    run_linear_probe_sweep(never_violates, start_rate=1_000, step=500, ceiling=2_500)

    assert calls == [1_000, 1_500, 2_000, 2_500]


def test_run_linear_probe_sweep_rejects_a_start_rate_above_the_ceiling():
    with pytest.raises(ValueError):
        run_linear_probe_sweep(lambda rate: None, start_rate=5_000, ceiling=1_000)


def test_run_linear_probe_sweep_rejects_a_non_positive_step():
    with pytest.raises(ValueError):
        run_linear_probe_sweep(lambda rate: None, start_rate=1_000, step=0, ceiling=5_000)


def test_run_linear_probe_sweep_flags_unmeasured_generator_cpu():
    calls: list[int] = []

    def probe_fn(rate: int) -> ProbeResult:
        calls.append(rate)
        cpu = None if len(calls) == 2 else 30.0
        return ProbeResult(rate=rate, violated_slo=rate >= 4_000, generator_cpu_percent=cpu)

    result = run_linear_probe_sweep(probe_fn, start_rate=1_000, step=1_000, ceiling=50_000)

    assert result.generator_cpu_unmeasured is True
    assert result.loadgen_bottleneck is False
