"""Testes de analysis/fetch_results.py — montagem do comando de download.

Nenhuma chamada a gcloud: as asserções são sobre o argv, mesma disciplina dos
testes dos construtores de comando remoto em
infra/scripts/tests/test_run_measurement_battery.py."""

from __future__ import annotations

from analysis.fetch_results import ESTRESSE_PREFIX, build_rsync_command


def test_source_and_destination_use_the_isolated_prefix():
    cmd = build_rsync_command("tcc-results", "results")

    assert f"gs://tcc-results/{ESTRESSE_PREFIX}/" in cmd
    assert f"results/{ESTRESSE_PREFIX}/" in cmd
    # O prefixo existe para o relatório principal nunca enxergar esta
    # campanha: `results/_estresse/<cell>/...` não casa com o glob
    # `results/*/{triagem,confirmacao}/**` de analysis/report.py.
    assert ESTRESSE_PREFIX.startswith("_")


def test_never_passes_a_delete_flag():
    # A campanha não pode apagar nada — nem no bucket, nem no destino local,
    # onde convivem os resultados da campanha principal.
    for include_raw in (False, True):
        cmd = build_rsync_command("tcc-results", "results", include_raw=include_raw)
        assert not any("delete" in arg for arg in cmd)


def test_raw_files_are_excluded_by_default():
    cmd = build_rsync_command("tcc-results", "results")
    exclude = [arg for arg in cmd if arg.startswith("--exclude=")]

    assert len(exclude) == 1
    for raw in ("requests", "k6-raw", "proc_stat"):
        assert raw in exclude[0]


def test_include_raw_drops_the_exclusion():
    cmd = build_rsync_command("tcc-results", "results", include_raw=True)

    assert not any(arg.startswith("--exclude=") for arg in cmd)


def test_trailing_slash_in_local_root_does_not_double_up():
    cmd = build_rsync_command("tcc-results", "results/")

    assert f"results/{ESTRESSE_PREFIX}/" in cmd
    assert f"results//{ESTRESSE_PREFIX}/" not in cmd


def test_command_is_argv_not_a_shell_string():
    # String concatenada é como se injeta argumento sem querer; o projeto
    # monta todo comando como lista.
    cmd = build_rsync_command("tcc-results", "results")

    assert isinstance(cmd, list)
    assert all(isinstance(arg, str) for arg in cmd)
    assert cmd[:3] == ["gcloud", "storage", "rsync"]


def test_recursive_is_always_requested():
    # Sem --recursive o rsync não desce em <cell>/<timestamp>/ e não traria
    # nada útil.
    assert "--recursive" in build_rsync_command("tcc-results", "results")
