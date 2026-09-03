"""Testes das funções puras de infra/scripts/cloud_smoke_test.py — a
orquestração real (terraform/gcloud/SSH) não é testada aqui, mesmo padrão de
infra/scripts/tests/test_run_measurement_battery.py."""

from __future__ import annotations

from infra.scripts.cloud_smoke_test import (
    build_remote_schema_fixture_script,
    build_remote_verification_script,
)

_ARGS = ("e1-postgres", "postgres", "10.0.0.2", "10.0.0.3", "gcr.io/x/tools:1", "hunter2")


def test_schema_fixture_script_has_no_verification_steps():
    # restart_container() roda ENTRE este script e o de verificação (ver
    # main()) — se um gate de corretude vazasse pra cá, ele rodaria contra
    # um tcc-service cujo catálogo ainda não foi recarregado.
    cmd = build_remote_schema_fixture_script(*_ARGS)
    assert "load_oracle_fixture.py" in cmd
    assert "harness.verify_cli" not in cmd
    assert "test_service_smoke.py" not in cmd
    assert "k6 run" not in cmd


def test_verification_script_has_no_schema_fixture_steps():
    # Estes gates só fazem sentido depois do restart_container() — rodar o
    # schema/fixture aqui de novo seria redundante e mascararia a ordem.
    cmd = build_remote_verification_script(*_ARGS)
    assert "load_oracle_fixture.py" not in cmd
    assert "apply_schema.py" not in cmd
    assert "harness.verify_cli --cell e1-postgres" in cmd
    assert "test_service_smoke.py" in cmd
    assert "k6 run" in cmd


def test_verification_script_smoke_report_reads_the_k6_console_output():
    cmd = build_remote_verification_script(*_ARGS)
    assert "analysis/smoke_report.py /tmp/smoke-requests.ndjson" in cmd
