#!/bin/bash
# Snapshot do disco de dados depois de carregar a massa numa célula —
# IMPLEMENTACAO.md, "Custo": "Carregar 100M registros leva horas; sem
# snapshot, religar o ambiente repete o custo." Chamado pelo fluxo do
# Makefile, nunca por `local-exec` do Terraform (carga de dados é lógica
# de aplicação, proibida ali — IMPLEMENTACAO.md, "Práticas obrigatórias").
#
# Uso: infra/scripts/snapshot_after_load.sh <disk-name> <zone> <project-id>
# (disk-name = output `data_disk_name` de infra/envs/experiment)

set -euo pipefail

DISK_NAME="${1:?uso: snapshot_after_load.sh <disk-name> <zone> <project-id>}"
ZONE="${2:?uso: snapshot_after_load.sh <disk-name> <zone> <project-id>}"
PROJECT_ID="${3:?uso: snapshot_after_load.sh <disk-name> <zone> <project-id>}"
SNAPSHOT_NAME="${DISK_NAME}-$(date -u +%Y%m%dt%H%M%Sz)"

gcloud compute disks snapshot "$DISK_NAME" \
  --zone="$ZONE" \
  --project="$PROJECT_ID" \
  --snapshot-names="$SNAPSHOT_NAME"

echo "Snapshot criado: $SNAPSHOT_NAME"
