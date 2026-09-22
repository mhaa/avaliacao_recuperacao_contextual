"""Testes do pré-voo de disco de load/ramp.py.

Este portão existe porque a proteção de disco do caminho principal — upload e
`unlink` do `k6-raw.json` a cada repetição — não dispara numa rampa contínua,
que tem uma única execução do k6. O modo de falha sem ele já aconteceu ao vivo
em e2-opensearch: "no space left on device", NDJSON truncado e JSONDecodeError
só no parse, depois de a execução inteira ter sido paga."""

from __future__ import annotations

import pytest

from load.ramp import (
    DEFAULT_DISK_BUDGET_FRACTION,
    NDJSON_BYTES_PER_REQUEST,
    RampStep,
    build_step_schedule,
    check_disk_budget,
    estimated_ndjson_bytes,
    estimated_request_count,
)

GB = 1_000_000_000


def test_estimated_bytes_uses_the_measured_line_size():
    # 134 B/requisição não é chute: results/e4-valkey/.../requests.ndjson tem
    # 5.618.470 bytes em 42.002 linhas.
    schedule = [RampStep(1000, "fine_up", 60)]
    assert estimated_ndjson_bytes(schedule) == 60_000 * NDJSON_BYTES_PER_REQUEST


def test_budget_accepts_a_schedule_just_below_the_threshold():
    schedule = [RampStep(1000, "fine_up", 60)]
    needed = estimated_ndjson_bytes(schedule)
    free = int(needed / DEFAULT_DISK_BUDGET_FRACTION) + 1

    check_disk_budget(schedule, free)  # não levanta


def test_budget_refuses_a_schedule_just_above_the_threshold():
    schedule = [RampStep(1000, "fine_up", 60)]
    needed = estimated_ndjson_bytes(schedule)
    free = int(needed / DEFAULT_DISK_BUDGET_FRACTION) - 1

    with pytest.raises(ValueError, match="excede"):
        check_disk_budget(schedule, free)


def test_refusal_message_names_the_knobs_that_fix_it():
    # A mensagem é lida por quem está prestes a gastar dinheiro em VMs; dizer
    # só "não cabe" obrigaria a ir ler o código para saber o que ajustar.
    # 50.000 req/s por 60 s = 3 M requisições = ~0,40 GB, contra um orçamento
    # de 50% de 0,1 GB.
    schedule = [RampStep(50_000, "fine_up", 60)]
    with pytest.raises(ValueError) as excinfo:
        check_disk_budget(schedule, free_bytes=GB // 10)

    message = str(excinfo.value)
    assert "--ramp-fine-step" in message
    assert "--ramp-knee" in message
    assert "loadgen" in message


def test_e2_scylla_sized_ramp_fits_the_current_100gb_generator_disk():
    # Lote 1 do plano: joelho projetado ~11.300 req/s, passo fino de 100.
    schedule = build_step_schedule(knee=11_300, fine_step=100)
    check_disk_budget(schedule, free_bytes=100 * GB)  # não levanta

    # Ordem de grandeza registrada para o caso de o cronograma mudar sem que
    # ninguém reveja o dimensionamento do disco.
    assert estimated_ndjson_bytes(schedule) < 20 * GB


def test_e3_valkey_sized_ramp_needs_the_coarser_step():
    # Por que o Lote 2 usa passo de 400 e não de 100: o mesmo joelho com passo
    # fino de 100 multiplica os degraus e estoura o orçamento do disco.
    coarse = build_step_schedule(knee=38_300, fine_step=400)
    fine = build_step_schedule(knee=38_300, fine_step=100)

    assert estimated_ndjson_bytes(fine) > estimated_ndjson_bytes(coarse)
    with pytest.raises(ValueError):
        check_disk_budget(fine, free_bytes=100 * GB)


def test_request_count_is_a_ceiling_not_a_prediction():
    # Σ(taxa × duração) assume entrega integral da carga ofertada. Passado o
    # joelho o k6 descarta chegadas, então o volume real fica abaixo — teto é
    # exatamente o que se quer para dimensionar disco.
    schedule = build_step_schedule(knee=5_000, fine_step=500)
    total = estimated_request_count(schedule)

    assert total == sum(s.rate * s.duration_s for s in schedule)
