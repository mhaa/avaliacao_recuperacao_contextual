"""Testes do parser de CPU por núcleo (analysis/ramp_resources.py).

O caso central é o do Valkey: saturado, ele ocupa UM núcleo inteiro e o
agregado da VM fica em ~12,5% num host de 8 vCPU. É por isso que
`classify_bottleneck` nunca acusaria `database_cpu` nessas células, e é o que
este parser existe para tornar visível."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

from analysis.ramp_resources import (
    max_core_percent,
    parse_proc_stat_log,
    samples_from_log,
    write_db_cpu_cores_csv,
)

T0 = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(seconds=5)


def _cpu_line(name, *, busy, idle):
    # user nice system idle iowait irq softirq steal
    return f"{name} {busy} 0 0 {idle} 0 0 0 0"


def _snapshot(timestamp, cores):
    lines = [f"=== {timestamp.isoformat()}"]
    lines += [_cpu_line(name, busy=busy, idle=idle) for name, (busy, idle) in cores.items()]
    # Ruído real de /proc/stat: o amostrador grava o arquivo inteiro.
    lines += ["intr 12345 0 0", "ctxt 98765", "btime 1700000000", "processes 4242"]
    return "\n".join(lines)


def _log(cores_before, cores_after):
    return _snapshot(T0, cores_before) + "\n" + _snapshot(T1, cores_after) + "\n"


def test_one_saturated_core_among_eight_idle_ones():
    # A assinatura do Valkey. cpu0 vai de 0 para 500 jiffies ocupados sem
    # nenhum ocioso novo; os outros sete só acumulam ocioso.
    before = {"cpu": (0, 4000), **{f"cpu{i}": (0, 500) for i in range(8)}}
    after = {
        "cpu": (500, 7500),
        "cpu0": (500, 500),
        **{f"cpu{i}": (0, 1000) for i in range(1, 8)},
    }

    samples = samples_from_log(_log(before, after))
    by_core = {s.core: s.cpu_percent for s in samples}

    assert by_core["cpu0"] == 100.0
    assert all(by_core[f"cpu{i}"] == 0.0 for i in range(1, 8))
    # O agregado — o único número que classify_bottleneck enxerga — esconde
    # o núcleo saturado atrás de uma média de 12,5%.
    assert by_core["cpu"] == 12.5
    assert max_core_percent(samples) == 100.0


def test_max_core_percent_ignores_the_aggregate_line():
    # Se a linha `cpu` entrasse na conta, o máximo por núcleo seria apenas o
    # agregado de novo em metade dos casos — e o parser não serviria para
    # nada.
    before = {"cpu": (0, 100), "cpu0": (0, 50), "cpu1": (0, 50)}
    after = {"cpu": (100, 100), "cpu0": (100, 50), "cpu1": (0, 100)}

    samples = samples_from_log(_log(before, after))

    assert max_core_percent(samples) == 100.0
    assert {s.core for s in samples} == {"cpu", "cpu0", "cpu1"}


def test_fully_idle_cpu_reads_zero_not_none():
    before = {"cpu0": (100, 900)}
    after = {"cpu0": (100, 1900)}

    assert samples_from_log(_log(before, after))[0].cpu_percent == 0.0


def test_snapshot_pair_with_no_elapsed_jiffies_is_zero_not_a_division_error():
    before = {"cpu0": (100, 900)}
    after = {"cpu0": (100, 900)}

    assert samples_from_log(_log(before, after))[0].cpu_percent == 0.0


def test_counter_that_went_backwards_is_clamped_not_reported_above_100():
    # Jiffies acumulados nunca deveriam retroceder, mas migração de VM ao
    # vivo e reinício de contador acontecem. Sem o clamp isto vira "114% de
    # CPU" entrando calado na figura do TCC — encontrado num ensaio ponta a
    # ponta sintético.
    before = {"cpu0": (0, 100)}
    after = {"cpu0": (800, 0)}

    assert samples_from_log(_log(before, after))[0].cpu_percent == 100.0


def test_clamp_does_not_distort_ordinary_readings():
    # Metade ocupado tem de continuar 50%, não ser arredondado pelo clamp.
    before = {"cpu0": (0, 0)}
    after = {"cpu0": (50, 50)}

    assert samples_from_log(_log(before, after))[0].cpu_percent == 50.0


def test_non_cpu_lines_are_ignored():
    snapshots = parse_proc_stat_log(_log({"cpu0": (0, 100)}, {"cpu0": (50, 150)}))

    assert len(snapshots) == 2
    assert set(snapshots[0].per_core) == {"cpu0"}


def test_a_single_snapshot_yields_no_samples():
    # Uma leitura só não define uma taxa: CPU vem de DELTA entre duas.
    assert samples_from_log(_snapshot(T0, {"cpu0": (0, 100)})) == []


def test_core_that_appears_only_in_the_later_snapshot_is_skipped():
    # Sem leitura anterior não há delta; inventar uma base zeraria a conta e
    # reportaria 100% falso.
    log = (
        _snapshot(T0, {"cpu0": (0, 100)})
        + "\n"
        + _snapshot(T1, {"cpu0": (50, 150), "cpu1": (10, 10)})
    )

    assert {s.core for s in samples_from_log(log)} == {"cpu0"}


def test_samples_are_stamped_with_the_end_of_their_window():
    # Carimbar com o início atribuiria ao degrau anterior uma janela que já
    # pertence ao seguinte, no alinhamento por intervalo.
    samples = samples_from_log(_log({"cpu0": (0, 100)}, {"cpu0": (50, 150)}))

    assert samples[0].timestamp == T1


def test_csv_round_trips_with_the_documented_columns(tmp_path):
    samples = samples_from_log(_log({"cpu0": (0, 100)}, {"cpu0": (100, 100)}))
    out = tmp_path / "db_cpu_cores.csv"

    write_db_cpu_cores_csv(samples, out)
    rows = list(csv.DictReader(out.open()))

    assert list(rows[0]) == ["timestamp", "core", "cpu_percent"]
    assert rows[0]["core"] == "cpu0"
    assert float(rows[0]["cpu_percent"]) == 100.0
