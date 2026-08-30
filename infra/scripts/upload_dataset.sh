#!/bin/bash
# Envia a massa de dados completa (escala real, gerada por
# `docker compose run --rm generator all --seed 42`, sem --sample-users)
# para o bucket de dataset do bootstrap — Fase 5 (README.md). Roda uma vez
# só: o dataset é compartilhado pelas 14 células, cada VM `loadgen` baixa
# daqui via harness/fixtures.py:ensure_full_dataset_downloaded. Deliberadamente
# fora do Terraform (mesmo motivo de build_and_push_images.sh: dado gerado
# localmente, não um recurso de infraestrutura). Roda no host com a sessão
# pessoal do usuário (`gcloud auth login`), não a service account do
# Terraform.
#
# Uso: infra/scripts/upload_dataset.sh <dataset-bucket>
# Exemplo: infra/scripts/upload_dataset.sh meu-projeto-tcc-dataset

set -euo pipefail

DATASET_BUCKET="${1:?uso: upload_dataset.sh <dataset-bucket>}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DATA_DIR="${REPO_ROOT}/data_generation/data"

if [[ ! -d "${DATA_DIR}/candidates.parquet" || ! -f "${DATA_DIR}/prematerialized.parquet" ]]; then
  echo "ERRO: ${DATA_DIR} não parece ter a massa de dados gerada." >&2
  echo "Rode primeiro: docker compose run --rm generator all --seed 42" >&2
  exit 1
fi

echo "Enviando candidates.parquet/ -> gs://${DATASET_BUCKET}/dataset/candidates.parquet/"
gcloud storage cp --recursive "${DATA_DIR}/candidates.parquet" "gs://${DATASET_BUCKET}/dataset/"

echo "Enviando prematerialized.parquet -> gs://${DATASET_BUCKET}/dataset/prematerialized.parquet"
gcloud storage cp "${DATA_DIR}/prematerialized.parquet" "gs://${DATASET_BUCKET}/dataset/"

echo
echo "Pronto. Use dataset_bucket=${DATASET_BUCKET} nas chamadas de"
echo "infra.scripts.run_measurement_battery (Fase 5)."
