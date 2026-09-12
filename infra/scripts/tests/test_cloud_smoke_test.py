"""Testes das funções puras de infra/scripts/cloud_smoke_test.py — a
orquestração real (terraform/gcloud/SSH) não é testada aqui, mesmo padrão de
infra/scripts/tests/test_run_measurement_battery.py."""

from __future__ import annotations

import subprocess

import infra.scripts.cloud_smoke_test as cloud_smoke_test
from infra.scripts.cloud_smoke_test import (
    _confirm_billable,
    build_remote_schema_fixture_script,
    build_remote_verification_script,
    gcloud_ssh_with_retry,
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


def test_confirm_billable_auto_approve_skips_input(monkeypatch):
    # auto_approve=True precisa pular o input() bloqueante (é o ponto todo
    # da flag --yes) sem abortar — se input() for chamado aqui, o teste
    # falha por conta própria (nenhum stdin disponível).
    def _fail_if_called(prompt):
        raise AssertionError("input() não deveria ser chamado com auto_approve=True")

    monkeypatch.setattr("builtins.input", _fail_if_called)
    _confirm_billable("mensagem de teste", auto_approve=True)


def test_confirm_billable_default_still_prompts(monkeypatch):
    # Comportamento default (sem a flag) tem que continuar bloqueando em
    # input() — auto_approve não pode virar o novo default por engano.
    calls = []

    def _record(prompt):
        calls.append(prompt)
        return "sim"

    monkeypatch.setattr("builtins.input", _record)
    _confirm_billable("mensagem de teste")
    assert len(calls) == 1


def test_gcloud_ssh_with_retry_succeeds_after_transient_failures(monkeypatch):
    # A queda real ("Remote side unexpectedly closed network connection")
    # é transiente — a 2ª ou 3ª tentativa do MESMO comando costuma passar.
    attempts = []

    def _fake_gcloud_ssh(instance, zone, project_id, remote_command):
        attempts.append(remote_command)
        if len(attempts) < 3:
            raise subprocess.CalledProcessError(1, ["gcloud"])
        return subprocess.CompletedProcess(["gcloud"], 0, stdout="ok", stderr="")

    monkeypatch.setattr(cloud_smoke_test, "gcloud_ssh", _fake_gcloud_ssh)
    result = gcloud_ssh_with_retry(
        "loadgen", "us-east4-c", "proj", "echo oi", max_attempts=3, backoff_seconds=0
    )
    assert result.stdout == "ok"
    assert len(attempts) == 3


def test_gcloud_ssh_with_retry_reraises_after_exhausting_attempts(monkeypatch):
    def _always_fails(instance, zone, project_id, remote_command):
        raise subprocess.CalledProcessError(1, ["gcloud"])

    monkeypatch.setattr(cloud_smoke_test, "gcloud_ssh", _always_fails)
    try:
        gcloud_ssh_with_retry(
            "loadgen", "us-east4-c", "proj", "echo oi", max_attempts=2, backoff_seconds=0
        )
        assert False, "deveria ter relançado CalledProcessError"
    except subprocess.CalledProcessError:
        pass
