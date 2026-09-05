"""Sobe `results/<cell>/...` direto da VM loadgen para o bucket de
resultados — substitui o hop `gcloud compute scp` (loadgen -> host) seguido
de `gcloud storage cp` (host -> bucket) que `run_measurement_battery.py`
usava antes. No Windows, `gcloud compute scp` roda sobre `pscp`/`plink`
(PuTTY), cujo cliente SFTP é menos robusto que o do OpenSSH sobre uma
transferência longa através do túnel IAP — confirmado ao vivo: um
`k6-raw.json` de ~35MB abortou a meio de "FATAL ERROR: unable to understand
SFTP response packet from server: unexpected OK response" em e3-postgres,
depois de já ter iniciado o `terraform destroy` do lado de fora falhar por
um problema totalmente não relacionado (Docker Desktop local), deixando as
3 VMs órfãs cobrando até intervenção manual.

Credenciais via ADC (metadata server) — mesmo padrão de
harness/fixtures.py:ensure_full_dataset_downloaded. Só funciona dentro de um
`docker run --network host`, onde o cliente enxerga o metadata server da VM.

Uso: python load/upload_results.py <local_dir> <bucket> <prefix>
Sobe todo o conteúdo (recursivo) de <local_dir> para gs://<bucket>/<prefix>/,
preservando a estrutura de subpastas.
"""

from __future__ import annotations

import sys
from pathlib import Path

from google.cloud import storage


def upload_directory(local_dir: Path, bucket_name: str, prefix: str) -> int:
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    count = 0
    for path in sorted(local_dir.rglob("*")):
        if path.is_file():
            rel = path.relative_to(local_dir).as_posix()
            bucket.blob(f"{prefix}/{rel}").upload_from_filename(str(path))
            count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print("uso: python load/upload_results.py <local_dir> <bucket> <prefix>", file=sys.stderr)
        return 1
    local_dir, bucket_name, prefix = args
    local_path = Path(local_dir)
    if not local_path.is_dir():
        print(f"ERRO: {local_dir} não existe ou não é diretório.", file=sys.stderr)
        return 1
    # flush=True: mesmo motivo de harness/fixtures.py — sem sinal de vida
    # nenhum durante um upload de vários GB seria indistinguível de travado.
    print(f"Enviando {local_dir} para gs://{bucket_name}/{prefix}/...", flush=True)
    count = upload_directory(local_path, bucket_name, prefix)
    print(f"{count} arquivo(s) enviados.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
