"""Testes das funções puras de infra/scripts/run_stress_ramp.py.

Como em test_run_measurement_battery.py, só os construtores de comando e a
aritmética são testados — nada que fale com gcloud, terraform ou k6 de
verdade. E, como lá, o valor está em travar as decisões que custam dinheiro
ou invalidam medição se regredirem."""

from __future__ import annotations

import json

import pytest

from infra.scripts.run_measurement_battery import build_remote_probe_rep_command
from infra.scripts.run_stress_ramp import (
    CELL_DEFAULTS,
    ESTRESSE_PREFIX,
    MACHINE_MEMORY_MB,
    PROBE_MEASURE,
    PROBE_WARMUP,
    PROC_STAT_REMOTE_PATH,
    TIER,
    build_proc_stat_sampler_command,
    build_remote_ramp_command,
    knee_from_search,
    schedule_duration_s,
)
from load.ramp import build_step_schedule, check_disk_budget

GB = 1_000_000_000


def _ramp_command(**overrides):
    kwargs = {
        "cell_id": "e2-scylla",
        "target_url": "http://10.0.0.5:8000/v1/recommendations",
        "tier": "medium",
        "stages_json": '[{"rate":1000,"phase":"fine_up","duration_s":60}]',
        "max_vus": 2000,
        "tools_image": "us-east4-docker.pkg.dev/proj/tcc/tools:latest",
        "results_mount": "/home/tcc/results",
        "fixtures_mount": "/home/tcc/load-fixtures",
        "remote_subdir": "_estresse/e2-scylla/20260922T120000Z",
        "user_count": 200_948,
    }
    kwargs.update(overrides)
    return build_remote_ramp_command(**kwargs)


def test_ramp_command_never_asks_k6_for_raw_json():
    # k6-raw.json pesa ~4x a NDJSON e a proteção que o contém (upload +
    # unlink por repetição) não existe numa rampa contínua — com ele,
    # e3-postgres e e3-valkey estourariam o disco de 100 GB.
    # `--out json=` é a flag do k6; o `--out` de analysis.ramp_report no
    # mesmo comando é outra coisa e precisa continuar lá.
    cmd = _ramp_command()
    assert "--out json=" not in cmd
    assert "k6-raw" not in cmd
    assert "analysis.ramp_report" in cmd


def test_ramp_command_enables_stress_mode_and_passes_the_schedule():
    cmd = _ramp_command()

    assert "STRESS_RAMP_MODE=true" in cmd
    assert "RAMP_STAGES=" in cmd
    assert "RAMP_MAX_VUS=2000" in cmd
    # PROBE_MODE ativaria o executor de patamar único, e o cronograma seria
    # ignorado sem erro nenhum.
    assert "PROBE_MODE" not in cmd


def test_ramp_command_runs_the_report_on_the_generator_vm():
    # O relatório nasce do lado que já sobe direto para o bucket — é o que
    # evita transitar até 43 GB de NDJSON pelo host.
    cmd = _ramp_command()

    assert "analysis.ramp_report" in cmd
    assert f"ramp_{TIER}.json" in cmd


def test_ramp_command_writes_under_the_isolated_prefix():
    cmd = _ramp_command()

    assert ESTRESSE_PREFIX in cmd
    # Nunca sob results/<cell>/<phase>/, que é o que analysis/report.py varre.
    assert "/confirmacao/" not in cmd
    assert "/triagem/" not in cmd


def test_ramp_command_brackets_the_run_with_generator_cpu_readings():
    # Sem as duas leituras não há como avaliar o portão dos 60%, e uma curva
    # limitada pelo gerador passaria por dado do banco.
    cmd = _ramp_command()

    assert "gen_stat_before.txt" in cmd
    assert "gen_stat_after.txt" in cmd


def test_ramp_command_mounts_fixtures_read_only():
    assert "/app/load/fixtures:ro" in _ramp_command()


def test_ramp_command_injects_the_real_user_population():
    # Confiar no default de load/zipf.js já fez a nuvem amostrar 10.000 dos
    # 200.948 usuários uma vez (docs/DESIGN.md).
    assert "USER_COUNT=200948" in _ramp_command()


def test_proc_stat_sampler_is_bounded_not_an_infinite_loop():
    # Laço infinito + pkill é frágil em Container-Optimized OS; com contagem
    # fixa o amostrador morre sozinho mesmo se a rampa abortar.
    cmd = build_proc_stat_sampler_command(duration_s=300, interval_s=5)

    assert "while true" not in cmd
    assert "-lt 60" in cmd  # 300s / 5s
    assert PROC_STAT_REMOTE_PATH in cmd


def test_proc_stat_sampler_avoids_utilities_absent_from_cos():
    cmd = build_proc_stat_sampler_command(duration_s=60)

    assert "seq " not in cmd
    assert "timeout " not in cmd


def test_proc_stat_sampler_emits_parseable_iso_timestamps():
    # O marcador `=== <iso8601>` é o que analysis/ramp_resources.py usa para
    # separar snapshots; um formato diferente silenciaria o parser.
    cmd = build_proc_stat_sampler_command(duration_s=60)

    assert "===" in cmd
    assert "+%Y-%m-%dT%H:%M:%S+00:00" in cmd


def test_proc_stat_sampler_never_requests_zero_iterations():
    assert "-lt 1" in build_proc_stat_sampler_command(duration_s=1, interval_s=5)


@pytest.mark.parametrize("cell", sorted(CELL_DEFAULTS))
def test_every_cell_default_fits_the_generator_disk(cell):
    # O pré-voo roda antes de provisionar qualquer VM; um default que já
    # nasce estourando o disco seria uma armadilha cara.
    defaults = CELL_DEFAULTS[cell]
    schedule = build_step_schedule(defaults["knee"], defaults["fine_step"])

    check_disk_budget(schedule, 100 * GB)  # não levanta


@pytest.mark.parametrize("cell", sorted(CELL_DEFAULTS))
def test_every_cell_default_machine_has_a_known_memory_ceiling(cell):
    # Sem o teto correto, classify_bottleneck compara contra 32 GB numa VM
    # de 96 GB e erra o veredito de gargalo.
    defaults = CELL_DEFAULTS[cell]

    assert defaults["service"] in MACHINE_MEMORY_MB
    assert defaults["loadgen"] in MACHINE_MEMORY_MB


@pytest.mark.parametrize("cell", sorted(CELL_DEFAULTS))
def test_schedule_descent_mirrors_ascent_for_every_cell(cell):
    # A histerese só é mensurável se as taxas coincidirem nos dois sentidos.
    defaults = CELL_DEFAULTS[cell]
    schedule = build_step_schedule(defaults["knee"], defaults["fine_step"])

    up = [s.rate for s in schedule if s.phase == "fine_up"]
    down = [s.rate for s in schedule if s.phase == "fine_down"]
    assert set(down).issubset(set(up))
    assert len(down) > 0


def test_schedule_duration_is_the_sum_of_step_durations():
    schedule = build_step_schedule(5_000, 500)
    assert schedule_duration_s(schedule) == sum(s.duration_s for s in schedule)


class _FakeSearch:
    def __init__(self, **kw):
        self.approx_throughput = kw.get("approx_throughput")
        self.censored = kw.get("censored", False)
        self.lower_bound = kw.get("lower_bound")
        self.loadgen_bottleneck = kw.get("loadgen_bottleneck", False)
        self.generator_cpu_unmeasured = False
        self.probes = []


def test_measured_knee_is_used_when_the_search_converges():
    knee, note = knee_from_search(_FakeSearch(approx_throughput=11750.0))

    assert knee == 11750
    assert note == ""


def test_generator_bottleneck_aborts_instead_of_yielding_a_knee():
    # Se o gerador saturou na sondagem, a rampa herdaria o mesmo teto e
    # mediria o gerador — não o banco. Devolver um número aqui seria
    # transformar execução inválida em dado.
    knee, note = knee_from_search(_FakeSearch(loadgen_bottleneck=True))

    assert knee is None
    assert "gerador" in note
    assert "--loadgen-machine-type" in note


def test_censored_search_uses_the_lower_bound_but_says_so():
    # O joelho existe acima do teto; montar a rampa no piso medido é melhor
    # que abortar, mas quem lê precisa saber que é piso, não medida.
    knee, note = knee_from_search(_FakeSearch(censored=True, lower_bound=50_000.0))

    assert knee == 50_000
    assert "censurado" in note


def test_search_without_a_throughput_is_refused():
    knee, note = knee_from_search(_FakeSearch())

    assert knee is None
    assert note


def test_probe_is_a_single_repetition_per_level():
    # Pedido explícito: sondagem rápida, "sem repetições do step". A
    # dispersão viria da fase fina da rampa, não daqui.
    cmd = build_remote_probe_rep_command(
        "e2-scylla",
        "http://10.0.0.5:8000/v1/recommendations",
        "medium",
        1000,
        PROBE_WARMUP,
        PROBE_MEASURE,
        "tools:latest",
        "/home/tcc/results",
        "/home/tcc/load-fixtures",
        f"{ESTRESSE_PREFIX}/e2-scylla/20260922T120000Z/probe/knee-0-1000",
        0,
        capture_before_stat=True,
        capture_after_stat=True,
        user_count=200_948,
    )

    assert "rep0" in cmd
    assert "rep1" not in cmd


def test_probe_warmup_is_real_traffic_not_idleness():
    # 0s de aquecimento faria a medição começar a frio e diluiria a CPU do
    # gerador com tempo parado, afrouxando o portão dos 60%.
    assert PROBE_WARMUP != "0s"


def test_probe_commands_stay_inside_the_stress_namespace(monkeypatch):
    # A alegação de isolamento precisa de prova: make_probe_fn da campanha
    # principal grava sob _saturation/, e esta sondagem não pode.
    import infra.scripts.run_stress_ramp as rsr

    sent: list[str] = []

    class _Completed:
        stdout = (
            "PROBE_RESULT violated_slo=False p99=3.0 error_rate=0.0 "
            "request_count=60000 offered_ratio=1.0 generator_cpu_percent=30.0"
        )

    def _fake_ssh(instance, zone, project_id, command):
        sent.append(command)
        return _Completed()

    monkeypatch.setattr(rsr, "gcloud_ssh_with_retry", _fake_ssh)

    probe_fn = rsr.make_stress_probe_fn(
        "e2-scylla",
        "http://10.0.0.5:8000/v1/recommendations",
        "medium",
        "tcc-e2-scylla-loadgen-st",
        "us-east4-a",
        "proj",
        "tools:latest",
        "bucket",
        "20260922T120000Z",
        user_count=200_948,
    )
    result = probe_fn(4000)

    assert result.rate == 4000
    assert result.violated_slo is False
    assert sent, "a sondagem precisa emitir comandos remotos"
    assert all(ESTRESSE_PREFIX in c for c in sent)
    assert not any("_saturation/" in c for c in sent)


def test_probe_uploads_and_deletes_its_raw_json(monkeypatch):
    # A 32k req/s cada k6-raw.json passa de 1 GB; acumulá-los comeria o
    # disco que a NDJSON da rampa precisa.
    import infra.scripts.run_stress_ramp as rsr

    sent: list[str] = []

    class _Completed:
        stdout = "PROBE_RESULT violated_slo=True p99=900.0 error_rate=0.0 request_count=1 generator_cpu_percent=20.0"

    monkeypatch.setattr(
        rsr, "gcloud_ssh_with_retry", lambda i, z, p, c: (sent.append(c), _Completed())[1]
    )

    rsr.make_stress_probe_fn(
        "e2-scylla", "http://x", "medium", "vm", "z", "proj", "img", "bucket",
        "20260922T120000Z", user_count=10,
    )(1000)

    rep_cmd = sent[0]
    assert "upload_one_file.py" in rep_cmd
    assert "rm -f" in rep_cmd or "rm" in rep_cmd


def test_stages_json_round_trips_into_the_command():
    stages = [{"rate": 1000, "phase": "fine_up", "duration_s": 60}]
    cmd = _ramp_command(stages_json=json.dumps(stages, separators=(",", ":")))

    assert "rate" in cmd and "duration_s" in cmd
