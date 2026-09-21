"""Testa analysis/report.py contra uma árvore results/ sintética (3 células
fictícias, uma com latência deslocada de propósito) — mesma disciplina de
dados sintéticos com propriedade conhecida de analysis/tests/test_stats.py,
e o mesmo formato de NDJSON de analysis/tests/test_collect.py, sem depender
de k6 nem de uma medição real."""

from __future__ import annotations

import json

import numpy as np
import pytest

from analysis.report import (
    DISK_USD_PER_GB_MONTH,
    build_report,
    discover_rep_dirs,
    ensure_collected,
    load_cell_latencies,
    load_cell_returned_counts,
    load_cell_saturation,
    returned_count_stats,
    storage_medium_for_cell,
    unit_storage_cost_usd_month,
)


def _request_line(
    scenario: str, time: str, latency_ms: float, status: int, request_id: str, returned_count: int = 20
) -> str:
    return json.dumps(
        {
            "request_id": request_id,
            "scenario": scenario,
            "timestamp": time,
            "latency_ms": latency_ms,
            "status": status,
            "returned_count": returned_count,
        }
    )


def _write_fake_run(
    rep_dir,
    latencies: list[float],
    cell_id: str,
    returned_counts: list[int] | None = None,
    k: int = 20,
    selectivity_tier: str = "medium",
) -> None:
    rep_dir.mkdir(parents=True, exist_ok=True)
    # Default preserva o comportamento antigo (returned_count=20 fixo, sem
    # respostas parciais) — testes que já chamam esta fixture sem os novos
    # parâmetros continuam exercitando exatamente o mesmo cenário.
    returned_counts = returned_counts or [20] * len(latencies)
    lines = []
    for i, (latency, returned_count) in enumerate(zip(latencies, returned_counts)):
        request_id = f"{i}-0"
        time = f"2026-01-01T00:02:{i % 60:02d}.000Z"
        lines.append(
            _request_line("measurement", time, latency, 200, request_id, returned_count)
        )
    (rep_dir / "requests.ndjson").write_text("\n".join(lines) + "\n")
    # load_cell_latencies/load_cell_returned_counts leem tudo daqui, não da
    # profundidade do caminho (analysis/report.py) — precisa existir mesmo em
    # fixture sintética.
    (rep_dir / "manifest.json").write_text(
        json.dumps({"cell_id": cell_id, "k": k, "selectivity_tier": selectivity_tier})
    )


def _write_fake_storage_sizes(storage_root):
    """Fixture sintética pra storage_bytes_for_cell/unit_storage_cost_usd_month
    (analysis/report.py) — mesmo formato que
    infra/scripts/measure_storage_size.py grava de verdade em
    results/storage/<storage>.json, um valor plausível por
    tabela/padrão/índice usado pelas células e1/e2-postgres e e1-valkey
    (as únicas que os testes deste arquivo exercitam)."""
    storage_root.mkdir(parents=True, exist_ok=True)
    (storage_root / "postgres.json").write_text(
        json.dumps(
            {
                "backend": "postgres",
                "sizes": {
                    "candidates": 1000,
                    "item_contexts": 500,
                    "prematerialized": 300,
                    "inverted_lists": 100,
                },
            }
        )
    )
    (storage_root / "valkey.json").write_text(
        json.dumps(
            {
                "backend": "valkey",
                "sizes": {
                    "candidates:*": {"key_count": 10, "sampled": 10, "bytes_estimate": 2000},
                    "item_contexts:*": {"key_count": 10, "sampled": 10, "bytes_estimate": 500},
                    "candidates_set:*": {"key_count": 10, "sampled": 10, "bytes_estimate": 1500},
                    "inverted:*": {"key_count": 10, "sampled": 10, "bytes_estimate": 400},
                    "prematerialized:*": {"key_count": 10, "sampled": 10, "bytes_estimate": 300},
                },
            }
        )
    )


def _build_fake_results(tmp_path, phase="triagem"):
    rng = np.random.default_rng(42)
    cells = {
        "e1-postgres": rng.normal(10, 1, 100).tolist(),
        "e2-postgres": rng.normal(10, 1, 100).tolist(),
        "e1-valkey": rng.normal(30, 1, 100).tolist(),  # deslocada de propósito
    }
    for cell_id, latencies in cells.items():
        rep_dir = tmp_path / cell_id / phase / "20260101T000000Z" / "rep0"
        _write_fake_run(rep_dir, latencies, cell_id)
    return tmp_path, cells


def test_discover_rep_dirs_finds_every_cell_for_the_phase(tmp_path):
    results_root, cells = _build_fake_results(tmp_path)
    rep_dirs = discover_rep_dirs(results_root, "triagem")
    assert len(rep_dirs) == len(cells)
    assert not discover_rep_dirs(results_root, "confirmacao")


def test_ensure_collected_is_idempotent(tmp_path):
    results_root, _ = _build_fake_results(tmp_path)
    rep_dirs = discover_rep_dirs(results_root, "triagem")
    ensure_collected(rep_dirs)
    summary_mtimes = {p: (p / "summary.json").stat().st_mtime for p in rep_dirs}

    ensure_collected(rep_dirs)  # segunda chamada não deve recalcular

    for p in rep_dirs:
        assert (p / "summary.json").stat().st_mtime == summary_mtimes[p]


def test_load_cell_latencies_groups_by_cell(tmp_path):
    results_root, cells = _build_fake_results(tmp_path)
    rep_dirs = discover_rep_dirs(results_root, "triagem")
    ensure_collected(rep_dirs)

    groups = load_cell_latencies(rep_dirs)

    assert set(groups) == set(cells)
    assert len(groups["e1-postgres"]) == 100


def test_confirmacao_combos_do_not_collide_on_the_same_rep_numbers(tmp_path):
    """Regressão do bug real que derrubou e3-postgres em produção: a
    confirmação varre 9 combinações (rate, tier), cada uma com suas 5
    repetições. Sem o segmento <rate>-<tier>/ no caminho (load/run_battery.py:
    combo_out_dir), a 2ª combinação sobrescreveria rep0..rep4 da 1ª —
    localmente sem erro nenhum, e no bucket com 412 Precondition Failed
    (load/upload_one_file.py, if_generation_match=0). Aqui simula 2
    combinações da MESMA célula, MESMO timestamp, MESMOS números de
    repetição (rep0), só o segmento de combo diferente — como
    load/run_battery.py escreve de verdade — e confere que nenhuma
    sobrescreve a outra e que ambas entram no pool da célula."""
    base = tmp_path / "e3-postgres" / "confirmacao" / "20260101T000000Z"
    _write_fake_run(base / "100-low" / "rep0", [5.0] * 10, "e3-postgres")
    _write_fake_run(base / "10000-high" / "rep0", [50.0] * 10, "e3-postgres")

    rep_dirs = discover_rep_dirs(tmp_path, "confirmacao")
    assert len(rep_dirs) == 2  # nenhuma combinação sobrescreveu a outra

    ensure_collected(rep_dirs)
    groups = load_cell_latencies(rep_dirs)

    assert len(groups["e3-postgres"]) == 20  # as 2 combinações, não só a última
    assert sorted(groups["e3-postgres"]) == [5.0] * 10 + [50.0] * 10


def test_discover_rep_dirs_ignores_older_timestamps_for_the_same_cell(tmp_path):
    # Bug real: confirmacao_progress.log e o bucket de resultados acumulam
    # timestamps de tentativas antigas (retries, quedas de SSH, reexecuções
    # pós-fix de arquitetura) — confirmado ao vivo, as 4 células da campanha
    # de confirmação têm de 2 a 4 timestamps cada uma. Sem filtrar pelo mais
    # recente, load_cell_latencies misturaria a execução velha (aqui,
    # latência 999.0 — bem distante de tudo mais) com a válida.
    _write_fake_run(
        tmp_path / "e3-postgres" / "confirmacao" / "20260101T000000Z" / "1000-low" / "rep0",
        [999.0] * 5, "e3-postgres",
    )
    _write_fake_run(
        tmp_path / "e3-postgres" / "confirmacao" / "20260102T000000Z" / "1000-low" / "rep0",
        [10.0] * 5, "e3-postgres",
    )

    rep_dirs = discover_rep_dirs(tmp_path, "confirmacao")

    assert len(rep_dirs) == 1
    assert "20260102T000000Z" in str(rep_dirs[0])

    ensure_collected(rep_dirs)
    groups = load_cell_latencies(rep_dirs)
    assert groups["e3-postgres"] == [10.0] * 5


def test_discover_rep_dirs_keeps_older_tiers_rep_dirs_when_a_newer_timestamp_only_has_one_tier(
    tmp_path,
):
    # Regressão de uma re-medição escopada (--tier medium,
    # infra/scripts/run_measurement_battery.py): o timestamp novo só tem
    # combos de seletividade média. "Mais recente por célula" (o
    # comportamento antigo) apagaria high/low do timestamp velho do
    # relatório — precisa ser "mais recente por (célula, combo)".
    old_dir = tmp_path / "e3-postgres" / "confirmacao" / "20260101T000000Z"
    _write_fake_run(old_dir / "100-high" / "rep0", [1.0] * 5, "e3-postgres", selectivity_tier="high")
    _write_fake_run(old_dir / "100-medium" / "rep0", [2.0] * 5, "e3-postgres", selectivity_tier="medium")
    _write_fake_run(old_dir / "100-low" / "rep0", [3.0] * 5, "e3-postgres", selectivity_tier="low")

    new_dir = tmp_path / "e3-postgres" / "confirmacao" / "20260102T000000Z"
    _write_fake_run(
        new_dir / "100-medium" / "rep0", [99.0] * 5, "e3-postgres", selectivity_tier="medium"
    )

    rep_dirs = discover_rep_dirs(tmp_path, "confirmacao")
    combo_names = {p.parent.name for p in rep_dirs}

    assert combo_names == {"100-high", "100-medium", "100-low"}
    # A combinação de "medium" vem do timestamp NOVO, não do velho.
    medium_dir = next(p for p in rep_dirs if p.parent.name == "100-medium")
    assert "20260102T000000Z" in str(medium_dir)
    # high/low continuam vindo do timestamp antigo — nunca desaparecem.
    high_dir = next(p for p in rep_dirs if p.parent.name == "100-high")
    low_dir = next(p for p in rep_dirs if p.parent.name == "100-low")
    assert "20260101T000000Z" in str(high_dir)
    assert "20260101T000000Z" in str(low_dir)


def test_returned_count_stats_flags_partial_responses_by_selectivity_tier():
    # Metade das respostas vêm completas (k=20), metade parciais (5 itens) —
    # cenário de seletividade baixa filtrando demais os N candidatos.
    pairs = [(20, 20)] * 5 + [(5, 20)] * 5

    stats = returned_count_stats(pairs)

    assert stats["mean_returned_count"] == pytest.approx(12.5)
    assert stats["partial_response_rate"] == pytest.approx(0.5)
    assert stats["n"] == 10


def test_returned_count_stats_handles_an_empty_group_without_dividing_by_zero():
    stats = returned_count_stats([])

    assert stats == {"mean_returned_count": None, "partial_response_rate": None, "n": 0}


def test_load_cell_returned_counts_groups_by_cell_and_selectivity_tier(tmp_path):
    low_dir = tmp_path / "e1-postgres" / "confirmacao" / "20260101T000000Z" / "1000-low" / "rep0"
    high_dir = tmp_path / "e1-postgres" / "confirmacao" / "20260101T000000Z" / "1000-high" / "rep0"
    _write_fake_run(
        low_dir, [10.0] * 10, "e1-postgres",
        returned_counts=[20] * 5 + [5] * 5, k=20, selectivity_tier="low",
    )
    _write_fake_run(
        high_dir, [10.0] * 10, "e1-postgres",
        returned_counts=[20] * 10, k=20, selectivity_tier="high",
    )
    rep_dirs = discover_rep_dirs(tmp_path, "confirmacao")
    ensure_collected(rep_dirs)

    by_key = load_cell_returned_counts(rep_dirs)

    assert set(by_key) == {"e1-postgres|low", "e1-postgres|high"}
    low_stats = returned_count_stats(by_key["e1-postgres|low"])
    high_stats = returned_count_stats(by_key["e1-postgres|high"])
    assert low_stats["partial_response_rate"] == pytest.approx(0.5)
    assert high_stats["partial_response_rate"] == 0.0


def test_build_report_rejects_h0_and_dunn_points_at_the_shifted_cell(tmp_path):
    results_root, cells = _build_fake_results(tmp_path)
    rep_dirs = discover_rep_dirs(results_root, "triagem")
    ensure_collected(rep_dirs)
    groups = load_cell_latencies(rep_dirs)
    returned_counts = load_cell_returned_counts(rep_dirs)
    storage_root = tmp_path / "storage"
    _write_fake_storage_sizes(storage_root)

    report = build_report(
        groups, storage_root=storage_root, returned_counts_by_key=returned_counts
    )

    # _build_fake_results grava returned_count=20/k=20 (default), sem
    # respostas parciais, na seletividade "medium" (default de _write_fake_run).
    assert report["returned_count_by_cell_tier"]["e1-postgres|medium"] == {
        "mean_returned_count": 20.0,
        "partial_response_rate": 0.0,
        "n": 100,
    }

    assert report["kruskal_wallis"]["reject_h0"] is True
    assert report["dunn_posthoc"]["e1-postgres|e1-valkey"] < 0.05
    assert report["dunn_posthoc"]["e2-postgres|e1-valkey"] < 0.05
    assert report["dunn_posthoc"]["e1-postgres|e2-postgres"] > 0.05
    # e1-valkey foi deslocada bem acima das outras duas (_build_fake_results):
    # A12 alto (perto de 1) diz "quase toda observação de e1-valkey excede
    # e1-postgres", não só "os dois diferem" — a magnitude que falta ao Dunn.
    assert report["vargha_delaney_a"]["e1-valkey|e1-postgres"] > 0.9
    assert report["vargha_delaney_a"]["e1-postgres|e1-valkey"] < 0.1
    assert abs(report["vargha_delaney_a"]["e1-postgres|e2-postgres"] - 0.5) < 0.1
    cell_ids = {c["cell_id"] for c in report["cells"]}
    assert cell_ids == set(cells)

    # Sem saturação nenhuma, NENHUMA célula pode ser posta no plano de custo
    # (S é indeterminado). Isso não pode explodir nem passar silencioso: a
    # estatística continua válida, o custo simplesmente não existe, e o
    # relatório tem de dizer isso em voz alta.
    assert all(c["cost_defined"] is False for c in report["cells"])
    assert report["pareto_frontier"] == []
    assert {c["cell_id"] for c in report["cells_without_cost"]} == set(cells)
    assert report["cost_model_warnings"]


def test_load_cell_saturation_uses_the_most_recent_timestamp_per_cell(tmp_path):
    old_dir = tmp_path / "e1-postgres" / "triagem" / "20260101T000000Z"
    new_dir = tmp_path / "e1-postgres" / "triagem" / "20260102T000000Z"
    (old_dir / "rep0").mkdir(parents=True)
    (new_dir / "rep0").mkdir(parents=True)
    (old_dir / "saturation.json").write_text(
        json.dumps({"approx_throughput": 1000.0, "censored": False, "lower_bound": None, "loadgen_bottleneck": False})
    )
    (new_dir / "saturation.json").write_text(
        json.dumps({"approx_throughput": 9000.0, "censored": False, "lower_bound": None, "loadgen_bottleneck": False})
    )

    result = load_cell_saturation(tmp_path, "triagem")

    assert result["e1-postgres"]["approx_throughput"] == 9000.0


def test_load_cell_saturation_finds_a_ramp_only_run_that_has_no_repetitions(tmp_path):
    """Regressão do caminho `--only-saturation`: a rampa é re-executada
    sozinha e grava um timestamp NOVO contendo só saturation.json, sem
    `rep*/`. Enquanto esta função derivava o diretório dos rep_dirs, esse
    arquivo novo era invisível e o relatório seguia usando o S antigo — sem
    aviso nenhum, com todo o custo calculado sobre o valor errado."""
    with_reps = tmp_path / "e1-postgres" / "triagem" / "20260101T000000Z"
    (with_reps / "rep0").mkdir(parents=True)
    (with_reps / "saturation.json").write_text(
        json.dumps({"approx_throughput": 1000.0, "censored": False, "lower_bound": None})
    )
    ramp_only = tmp_path / "e1-postgres" / "triagem" / "20260202T000000Z"
    ramp_only.mkdir(parents=True)
    (ramp_only / "saturation.json").write_text(
        json.dumps({"approx_throughput": 1750.0, "censored": False, "lower_bound": None})
    )

    result = load_cell_saturation(tmp_path, "triagem")

    assert result["e1-postgres"]["approx_throughput"] == 1750.0


def test_load_cell_saturation_reads_the_per_tier_files_confirmacao_writes(tmp_path):
    # Bug real: a rampa de confirmação grava saturation_<tier>.json (uma por
    # seletividade — rampa adaptativa, docs/DESIGN.md "Rampa de
    # confirmação"), não um saturation.json único. Sem tratar isso, este
    # dict voltava vazio pras 4 células de confirmação e o custo/Pareto do
    # relatório final saía ausente pra elas, silenciosamente.
    run_dir = tmp_path / "e3-postgres" / "confirmacao" / "20260101T000000Z"
    run_dir.mkdir(parents=True)
    (run_dir / "saturation_high.json").write_text(
        json.dumps({"approx_throughput": 4587.0, "censored": False, "lower_bound": None})
    )
    (run_dir / "saturation_medium.json").write_text(
        json.dumps({"approx_throughput": 3672.0, "censored": False, "lower_bound": None})
    )
    (run_dir / "saturation_low.json").write_text(
        json.dumps({"approx_throughput": 3317.0, "censored": False, "lower_bound": None})
    )

    result = load_cell_saturation(tmp_path, "confirmacao")

    # "high" representa a célula no nível de topo (usado por build_report
    # para custo/Pareto) — nenhuma seletividade se perde, todas ficam em
    # by_tier.
    assert result["e3-postgres"]["approx_throughput"] == 4587.0
    assert result["e3-postgres"]["by_tier"]["high"]["approx_throughput"] == 4587.0
    assert result["e3-postgres"]["by_tier"]["medium"]["approx_throughput"] == 3672.0
    assert result["e3-postgres"]["by_tier"]["low"]["approx_throughput"] == 3317.0


def test_load_cell_saturation_merges_by_tier_across_timestamps_independently(tmp_path):
    # Regressão de uma re-medição escopada (--tier medium): o timestamp novo
    # só grava saturation_medium.json. "Mais recente por célula" (o
    # comportamento antigo) faria esse timestamp "vencer" sozinho e apagar
    # high/low do relatório — inclusive o representante de topo (hardcoded
    # em by_tier.get("high", {})), que ficaria vazio (custo/Pareto
    # desaparecendo da célula inteira). Cada tier precisa vir do timestamp
    # mais novo QUE O CONTÉM, independentemente dos outros.
    old_dir = tmp_path / "e3-postgres" / "confirmacao" / "20260101T000000Z"
    old_dir.mkdir(parents=True)
    (old_dir / "saturation_high.json").write_text(
        json.dumps({"approx_throughput": 4587.0, "censored": False, "lower_bound": None})
    )
    (old_dir / "saturation_medium.json").write_text(
        json.dumps({"approx_throughput": 3672.0, "censored": False, "lower_bound": None})
    )
    (old_dir / "saturation_low.json").write_text(
        json.dumps({"approx_throughput": 3317.0, "censored": False, "lower_bound": None})
    )

    new_dir = tmp_path / "e3-postgres" / "confirmacao" / "20260102T000000Z"
    new_dir.mkdir(parents=True)
    (new_dir / "saturation_medium.json").write_text(
        json.dumps({"approx_throughput": 3900.0, "censored": False, "lower_bound": None})
    )

    result = load_cell_saturation(tmp_path, "confirmacao")

    # medium vem do timestamp novo (re-medido); high/low continuam vindo do
    # antigo — nenhum dos dois desaparece.
    assert result["e3-postgres"]["by_tier"]["medium"]["approx_throughput"] == 3900.0
    assert result["e3-postgres"]["by_tier"]["high"]["approx_throughput"] == 4587.0
    assert result["e3-postgres"]["by_tier"]["low"]["approx_throughput"] == 3317.0
    # Representante de topo (usado por build_report para custo/Pareto)
    # continua populado — nunca fica vazio.
    assert result["e3-postgres"]["approx_throughput"] == 4587.0


def test_build_report_computes_cost_per_million_requests_and_the_frontier(tmp_path):
    results_root, cells = _build_fake_results(tmp_path)
    rep_dirs = discover_rep_dirs(results_root, "triagem")
    ensure_collected(rep_dirs)
    groups = load_cell_latencies(rep_dirs)

    saturation_by_cell = {
        "e1-postgres": {"approx_throughput": 5000.0, "censored": False, "lower_bound": None},
        "e2-postgres": {"approx_throughput": 5000.0, "censored": False, "lower_bound": None},
        "e1-valkey": {"approx_throughput": None, "censored": True, "lower_bound": 50000.0},
    }
    storage_root = tmp_path / "storage"
    _write_fake_storage_sizes(storage_root)
    report = build_report(groups, saturation_by_cell, storage_root=storage_root)

    by_id = {c["cell_id"]: c for c in report["cells"]}
    assert by_id["e1-postgres"]["saturation_throughput_approx"] == 5000.0
    assert by_id["e1-valkey"]["saturation_censored"] is True
    # Censurada: fica no plano de custo (tem um TETO via o lower_bound), mas
    # sem estimativa pontual — cost_undefined_reason não a exclui.
    assert by_id["e1-valkey"]["cost_defined"] is True
    assert by_id["e1-valkey"]["cost_per_million_requests_usd"] is None
    assert by_id["e1-valkey"]["cost_per_million_requests_usd_bounds"]["low"] == 0.0

    assert by_id["e1-postgres"]["cost_per_million_requests_usd"] > 0
    assert isinstance(report["pareto_frontier"], list)
    assert isinstance(report["cheapest_cell_ids"], list)
    assert "censorship_warning" in report


def test_valkey_storage_goes_to_the_capacity_term_and_the_others_to_the_disk_parcel(tmp_path):
    """A única linha que faz o armazenamento discriminar as tecnologias — e
    que estava sem cobertura nenhuma."""
    storage_root = tmp_path / "storage"
    _write_fake_storage_sizes(storage_root)

    assert storage_medium_for_cell("e1-valkey") == "memory"
    assert storage_medium_for_cell("e1-postgres") == "disk"
    # Memória não tem preço por GiB: age via n(D) (⌈V_mem/M⌉), não via C_a.
    # Zero aqui NÃO quer dizer "armazenamento de graça".
    assert unit_storage_cost_usd_month("e1-valkey", storage_root) == 0.0
    assert unit_storage_cost_usd_month("e1-postgres", storage_root) > 0.0


def test_disk_parcel_is_monthly_not_hourly(tmp_path):
    """Trava a correção horário -> mensal: 10 GiB a US$ 0,187/GiB-mês são
    ~US$ 1,87/mês. Com a constante horária antiga davam ~US$ 0,0026, e a
    parcela de estoque sumia dentro do arredondamento do custo de computação."""
    storage_root = tmp_path / "storage"
    storage_root.mkdir(parents=True, exist_ok=True)
    (storage_root / "postgres.json").write_text(
        json.dumps({"backend": "postgres", "sizes": {"candidates": 10 * 1024**3}})
    )

    cost = unit_storage_cost_usd_month("e1-postgres", storage_root)

    assert cost == pytest.approx(10 * DISK_USD_PER_GB_MONTH)
    assert cost == pytest.approx(1.87, rel=1e-3)
