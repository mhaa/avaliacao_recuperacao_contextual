"""Testes de load/saturation.py — algoritmo de busca de vazão de
saturação. Sempre com um `probe_fn` falso (nunca rede/gcloud real), mesma
disciplina de storage/tests/fakes.py."""

from __future__ import annotations

from load.saturation import (
    GENERATOR_CPU_THRESHOLD,
    ProbeResult,
    doubling_sequence,
    fine_sequence,
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
