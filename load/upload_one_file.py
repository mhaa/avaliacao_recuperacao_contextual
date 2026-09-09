"""Sobe um único arquivo pro bucket de resultados — usado por
load/run_battery.py:run_k6 e infra/scripts/run_measurement_battery.py:
make_probe_fn para subir cada k6-raw.json individual logo após a
repetição/sondagem que o gerou, e então apagar a cópia local, em vez de
deixá-lo acumular no disco da loadgen até o upload final em bloco de
load/upload_results.py.

k6-raw.json (saída nativa de diagnóstico do k6, "--out json=...") pode
passar de ~5GB por repetição em patamares de carga alta; sem limpeza
incremental, a busca de saturação da confirmação (5 repetições por
sondagem, muitas sondagens até convergir) já encheu o disco de 100GB da
loadgen e derrubou e3-postgres com "no space left on device". Fica só no
bucket a partir de agora — nada no pipeline de análise o lê de volta do
disco local (analysis/report.py, docs/ARCHITECTURE.md), então preservar a
cópia é só para diagnóstico manual eventual, e só o bucket precisa guardá-la.

Credenciais via ADC (metadata server), mesmo padrão de upload_results.py.

Uso: python load/upload_one_file.py <local_path> <bucket> <blob_name>
"""

from __future__ import annotations

import sys
from pathlib import Path

from google.cloud import storage


def upload_one_file(local_path: Path, bucket_name: str, blob_name: str) -> None:
    # if_generation_match=0: exige que o objeto NÃO exista ainda (cada
    # blob_name é único por timestamp+rep, nunca reescrito de propósito) —
    # sem isso, o upload resumível do cliente GCS assume que pode estar
    # sobrescrevendo algo e pede storage.objects.delete, que a service
    # account da loadgen não tem (só roles/storage.objectCreator,
    # infra/modules/loadgen/main.tf:loadgen_results_writer). Confirmado ao
    # vivo: 403 "does not have storage.objects.delete access" sem isto.
    client = storage.Client()
    client.bucket(bucket_name).blob(blob_name).upload_from_filename(
        str(local_path), if_generation_match=0
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print("uso: python load/upload_one_file.py <local_path> <bucket> <blob_name>", file=sys.stderr)
        return 1
    local_path, bucket_name, blob_name = args
    path = Path(local_path)
    if not path.is_file():
        print(f"ERRO: {local_path} não existe ou não é arquivo.", file=sys.stderr)
        return 1
    upload_one_file(path, bucket_name, blob_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
