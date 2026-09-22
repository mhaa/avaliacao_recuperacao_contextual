"""Testes do join por intervalo entre séries temporais de recurso e degraus
da rampa (analysis/ramp_resources.py:align_samples_to_steps).

Nem `resources.csv` (30 s) nem `db_cpu_cores.csv` (5 s) sabem qual degrau
estava ativo — são séries temporais puras. Este join é o que permite ler "na
taxa em que a vazão dobra, a CPU do banco está em X%"."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from analysis.ramp_resources import DbCpuCoreSample, align_samples_to_steps

T0 = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)


def _at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _step(rate, phase, start_s, end_s):
    return {
        "rate": rate,
        "phase": phase,
        "started_at": _at(start_s).isoformat(),
        "ended_at": _at(end_s).isoformat(),
    }


def _sample(seconds, core="cpu0", percent=50.0):
    return DbCpuCoreSample(timestamp=_at(seconds), core=core, cpu_percent=percent)


def test_sample_inside_a_step_is_assigned_to_it():
    steps = [_step(1000, "fine_up", 0, 60), _step(1100, "fine_up", 61, 120)]
    aligned = align_samples_to_steps([_sample(30)], steps)

    assert len(aligned[(1000, "fine_up")]) == 1
    assert aligned[(1100, "fine_up")] == []


def test_sample_outside_every_window_is_discarded():
    # Cai no intervalo entre degraus (troca de estágio do k6) ou no
    # setup/teardown. Atribuí-la ao vizinho mais próximo inventaria dado.
    steps = [_step(1000, "fine_up", 0, 60), _step(1100, "fine_up", 100, 160)]
    aligned = align_samples_to_steps([_sample(80)], steps)

    assert all(samples == [] for samples in aligned.values())


def test_sample_on_a_shared_boundary_is_counted_once():
    # Janelas vizinhas podem encostar no mesmo instante; contar a amostra
    # duas vezes inflaria a CPU média de ambos os degraus.
    steps = [_step(1000, "fine_up", 0, 60), _step(1100, "fine_up", 60, 120)]
    aligned = align_samples_to_steps([_sample(60)], steps)

    total = sum(len(v) for v in aligned.values())
    assert total == 1


def test_same_rate_on_ascent_and_descent_are_distinct_keys():
    # A chave é (taxa, fase) justamente porque a descida repete as taxas da
    # subida — é o par que revela a histerese.
    steps = [_step(4000, "fine_up", 0, 60), _step(4000, "fine_down", 600, 660)]
    aligned = align_samples_to_steps([_sample(30), _sample(630)], steps)

    assert len(aligned[(4000, "fine_up")]) == 1
    assert len(aligned[(4000, "fine_down")]) == 1


def test_steps_without_timestamps_are_skipped_not_crashed():
    # Um degrau sem requisição nenhuma não tem janela; deve sumir do join em
    # vez de derrubá-lo.
    steps = [
        {"rate": 1000, "phase": "fine_up", "started_at": None, "ended_at": None},
        _step(1100, "fine_up", 0, 60),
    ]
    aligned = align_samples_to_steps([_sample(30)], steps)

    assert (1000, "fine_up") not in aligned
    assert len(aligned[(1100, "fine_up")]) == 1


def test_every_step_appears_in_the_result_even_with_no_samples():
    # Degrau sem amostra é informação (a telemetria não cobriu aquele
    # minuto), não ausência de degrau — precisa aparecer vazio, não sumir.
    steps = [_step(1000, "fine_up", 0, 60), _step(1100, "fine_up", 61, 120)]
    aligned = align_samples_to_steps([], steps)

    assert set(aligned) == {(1000, "fine_up"), (1100, "fine_up")}
    assert all(v == [] for v in aligned.values())


def test_multiple_cores_in_the_same_window_all_land_together():
    steps = [_step(1000, "fine_up", 0, 60)]
    samples = [_sample(30, core=f"cpu{i}", percent=float(i)) for i in range(8)]

    aligned = align_samples_to_steps(samples, steps)

    assert len(aligned[(1000, "fine_up")]) == 8
