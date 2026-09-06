#!/bin/bash
# Build + push das imagens service/tools para o Artifact Registry — Fase 4
# (README.md). Deliberadamente fora do Terraform (ver
# infra/modules/service/main.tf: "publicada em Artifact Registry fora deste
# [terraform]"). Roda no host (usa `docker` e `gcloud` diretamente, com a
# sessão pessoal do usuário via `gcloud auth login` — não a service account
# dedicada ao Terraform, que não deveria ter permissão de push de imagem).
#
# Uso: infra/scripts/build_and_push_images.sh <project-id> <region> [tag]
# Exemplo: infra/scripts/build_and_push_images.sh meu-projeto us-east4 latest

set -euo pipefail

PROJECT_ID="${1:?uso: build_and_push_images.sh <project-id> <region> [tag]}"
REGION="${2:?uso: build_and_push_images.sh <project-id> <region> [tag]}"
TAG="${3:-latest}"

REPO_ID="tcc"
HOST="${REGION}-docker.pkg.dev"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "Verificando repositório Artifact Registry '${REPO_ID}' em ${REGION}..."
if ! gcloud artifacts repositories describe "$REPO_ID" \
    --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "  não existe, criando..."
  gcloud artifacts repositories create "$REPO_ID" \
    --repository-format=docker \
    --location="$REGION" \
    --project="$PROJECT_ID" \
    --description="Imagens service/tools do experimento TCC recsys-retrieval"
fi

echo "Configurando autenticação Docker para ${HOST}..."
gcloud auth configure-docker "$HOST" --quiet

SERVICE_IMAGE="${HOST}/${PROJECT_ID}/${REPO_ID}/service:${TAG}"
TOOLS_IMAGE="${HOST}/${PROJECT_ID}/${REPO_ID}/tools:${TAG}"

echo "Build service -> ${SERVICE_IMAGE}"
docker build -f "${REPO_ROOT}/docker/Dockerfile.service" -t "$SERVICE_IMAGE" "$REPO_ROOT"

echo "Build tools -> ${TOOLS_IMAGE}"
docker build -f "${REPO_ROOT}/docker/Dockerfile.tools" -t "$TOOLS_IMAGE" "$REPO_ROOT"

echo "Push ${SERVICE_IMAGE}"
docker push "$SERVICE_IMAGE"

echo "Push ${TOOLS_IMAGE}"
docker push "$TOOLS_IMAGE"

echo
echo "Pronto. Cole em infra/envs/experiment/terraform.tfvars:"
echo "  service_image = \"${SERVICE_IMAGE}\""
echo "  tools_image   = \"${TOOLS_IMAGE}\""
