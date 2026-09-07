"""Testes das funções puras de load/run_battery.py — a execução real do k6
via subprocess não é testada aqui (mesmo padrão de
schemas/postgres/load_oracle_fixture.py: scripts de integração/orquestração
não ganham teste unitário para o `main()`, só para a lógica pura)."""

from __future__ import annotations

from pathlib import Path

import pytest

from load.run_battery import (
    build_k6_cmd,
    build_probe_k6_cmd,
    build_run_plan,
    list_viable_cell_ids,
    shuffled_cell_order,
    target_url_for,
)


def test_list_viable_cell_ids_matches_the_14_yaml_files():
    ids = list_viable_cell_ids()
    assert "_defaults" not in ids
    assert len(ids) == 14
    assert "e1-postgres" in ids
    assert "e4-scylla" not in ids  # inviável: sem primitiva de interseção
    assert "e3-opensearch" not in ids  # inviável: pré-materialização sem sentido


def test_shuffled_cell_order_is_deterministic_given_the_same_seed():
    cell_ids = [f"cell-{i}" for i in range(10)]
    assert shuffled_cell_order(cell_ids, seed=1) == shuffled_cell_order(cell_ids, seed=1)


def test_shuffled_cell_order_differs_across_seeds():
    cell_ids = [f"cell-{i}" for i in range(10)]
    assert shuffled_cell_order(cell_ids, seed=1) != shuffled_cell_order(cell_ids, seed=2)


def test_shuffled_cell_order_is_a_permutation_not_a_subset():
    cell_ids = [f"cell-{i}" for i in range(10)]
    assert sorted(shuffled_cell_order(cell_ids, seed=7)) == sorted(cell_ids)


def test_build_run_plan_runs_all_repetitions_of_a_cell_before_the_next():
    plan = build_run_plan(["a", "b"], repetitions=3)
    assert plan == [("a", 0), ("a", 1), ("a", 2), ("b", 0), ("b", 1), ("b", 2)]


def test_target_url_for_uses_per_cell_mapping_when_given():
    targets = {"e1-postgres": "http://cell-a", "e2-postgres": "http://cell-b"}
    assert target_url_for("e1-postgres", None, targets) == "http://cell-a"


def test_target_url_for_falls_back_to_shared_url():
    assert target_url_for("e1-postgres", "http://shared", None) == "http://shared"


def test_target_url_for_raises_without_either():
    with pytest.raises(ValueError):
        target_url_for("e1-postgres", None, None)


def test_build_k6_cmd_plain_has_no_smoke_flag():
    cmd = build_k6_cmd(
        Path("out/k6-raw.json"), "e1-postgres", "http://svc", 100, 20, "medium", False
    )
    assert "SMOKE_MODE=true" not in cmd


def test_build_k6_cmd_smoke_sets_smoke_mode_env_var():
    cmd = build_k6_cmd(
        Path("out/k6-raw.json"), "e1-postgres", "http://svc", 100, 20, "medium", True
    )
    assert "SMOKE_MODE=true" in cmd


def test_build_k6_cmd_injects_user_count_when_given():
    # docs/DESIGN.md, "Protocolo de medição": o Zipf amostra a base INTEIRA
    # do ambiente — sem USER_COUNT no ambiente do k6, load/zipf.js usa o
    # default dev-scale (10.000) e a medição real amostra ~5% da base.
    cmd = build_k6_cmd(
        Path("out/k6-raw.json"), "e1-postgres", "http://svc", 100, 20, "medium", False,
        user_count=200_948,
    )
    assert "USER_COUNT=200948" in cmd


def test_build_k6_cmd_without_user_count_omits_the_env_var():
    # Só o smoke (dev-scale, mesma base do default do zipf.js) roda sem —
    # main() recusa medição sem --user-count.
    cmd = build_k6_cmd(
        Path("out/k6-raw.json"), "e1-postgres", "http://svc", 100, 20, "medium", True
    )
    assert not any(a.startswith("USER_COUNT=") for a in cmd)


def test_build_probe_k6_cmd_sets_probe_mode_and_rate():
    cmd = build_probe_k6_cmd(
        Path("out/k6-raw.json"), "e1-postgres", "http://svc", 4000, "medium", "0s", "1m",
        user_count=200_948,
    )
    assert "PROBE_MODE=true" in cmd
    assert "PROBE_RATE=4000" in cmd
    assert "PROBE_WARMUP=0s" in cmd
    assert "PROBE_MEASURE=1m" in cmd
    assert "SELECTIVITY_TIER=medium" in cmd


def test_build_probe_k6_cmd_passes_through_warmup_and_measure_durations():
    cmd = build_probe_k6_cmd(
        Path("out/k6-raw.json"), "e1-postgres", "http://svc", 11000, "low", "2m", "3m",
        user_count=200_948,
    )
    assert "PROBE_WARMUP=2m" in cmd
    assert "PROBE_MEASURE=3m" in cmd


def test_build_probe_k6_cmd_always_injects_user_count():
    # Sondagem é sempre medição (o S dela entra no custo via n(D) = ⌈D/S⌉) —
    # user_count é keyword-only obrigatório, nunca um default silencioso.
    cmd = build_probe_k6_cmd(
        Path("out/k6-raw.json"), "e1-postgres", "http://svc", 4000, "medium", "0s", "1m",
        user_count=200_948,
    )
    assert "USER_COUNT=200948" in cmd
