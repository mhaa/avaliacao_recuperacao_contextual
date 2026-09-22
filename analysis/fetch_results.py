"""Baixa os resultados da campanha de estresse do bucket para o ambiente
local (docs/DESIGN.md, "Experimento complementar").

O bucket é o destino PRIMÁRIO: `analysis/ramp_report.py` roda na VM do
gerador e sobe seu artefato direto de lá, e os dois arquivos que nascem no
host (`resources.csv`, `db_cpu_cores.csv`) são enviados e removidos do
temporário. A cópia local legítima é sempre a que vem daqui.

**Por padrão baixa só os derivados.** A NDJSON por requisição soma de 2 a 28
GB por célula (load/ramp.py:estimated_ndjson_bytes) e não é necessária
localmente: a agregação por degrau já aconteceu na VM do gerador, e o que
sobra é `ramp_<tier>.json` com alguns KB. `--include-raw` traz tudo, para
quem precisa reprocessar — ao custo do egress correspondente.

Nunca apaga nada no destino: sem `--delete-unmatched-destination-objects`, o
rsync só acrescenta e sobrescreve o que mudou.
"""

from __future__ import annotations

import argparse
import subprocess

# Prefixo próprio, fora do namespace que analysis/report.py varre
# (`results/*/{triagem,confirmacao}/**`). Ver docs/DESIGN.md, "Isolamento":
# um resultado medido DE PROPÓSITO além da saturação nunca pode ser lido
# pelo relatório principal.
ESTRESSE_PREFIX = "_estresse"

# Arquivos brutos, excluídos por padrão. Regex de caminho completo, como o
# `gcloud storage rsync --exclude` espera.
RAW_EXCLUDE_PATTERN = r".*(requests\.ndjson|k6-raw\.json|proc_stat\.log)$"


def build_rsync_command(bucket: str, local_root: str, *, include_raw: bool = False) -> list[str]:
    """argv do `gcloud storage rsync`, montado como lista (nunca string
    concatenada), mesma disciplina dos construtores de comando remoto de
    infra/scripts/run_measurement_battery.py."""
    source = f"gs://{bucket}/{ESTRESSE_PREFIX}/"
    destination = f"{local_root.rstrip('/')}/{ESTRESSE_PREFIX}/"
    cmd = ["gcloud", "storage", "rsync", source, destination, "--recursive"]
    if not include_raw:
        cmd.append(f"--exclude={RAW_EXCLUDE_PATTERN}")
    return cmd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bucket", help="results-bucket da campanha (sem o gs://)")
    parser.add_argument(
        "--local-root",
        default="results",
        help="raiz local; o prefixo _estresse/ é acrescentado (default: results)",
    )
    parser.add_argument(
        "--include-raw",
        action="store_true",
        help="também baixa requests.ndjson/k6-raw.json/proc_stat.log — dezenas de GB e "
        "o egress correspondente. Só é necessário para reprocessar a rampa do zero.",
    )
    args = parser.parse_args()

    cmd = build_rsync_command(args.bucket, args.local_root, include_raw=args.include_raw)
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
