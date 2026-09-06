"""`resolve_snapshot_strategy` (infra/scripts/measure_storage_size.py) —
separada de `snapshot_exists()` (chamada real à GCP) pra ser testável sem
mock de API. O resto do script (terraform apply/destroy, SSH) não tem teste
próprio — mesma disciplina de infra/scripts/seed_dataset_snapshots.py, que
também não testa a orquestração fim-a-fim, só a lógica pura extraída dela.
"""

from __future__ import annotations

from infra.scripts.measure_storage_size import resolve_snapshot_strategy


def test_restores_from_snapshot_when_one_exists_for_a_disk_backed_storage():
    snapshot, skip = resolve_snapshot_strategy("postgres", "tcc-dataset-seed-postgres", True)

    assert snapshot == "tcc-dataset-seed-postgres"
    assert skip is True


def test_pays_full_load_when_no_snapshot_exists_yet_for_a_disk_backed_storage():
    snapshot, skip = resolve_snapshot_strategy("scylla", "tcc-dataset-seed-scylla", False)

    assert snapshot == ""
    assert skip is False


def test_valkey_always_pays_full_load_even_if_a_snapshot_name_was_somehow_passed():
    # Valkey nunca tem disco persistente (README.md) — não deveria nem
    # chegar aqui com snapshot_found=True na prática (main() nunca chama
    # snapshot_exists() pra valkey), mas a função continua correta mesmo
    # que isso mude.
    snapshot, skip = resolve_snapshot_strategy("valkey", "", True)

    assert snapshot == ""
    assert skip is False
