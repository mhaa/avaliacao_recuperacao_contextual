#!/bin/bash
# Minta um token de acesso de curta duração (~1h) impersonando a service
# account do Terraform (terraform-tcc@<project-id>.iam.gserviceaccount.com,
# criada por infra/scripts/create_terraform_service_account.sh) e roda o
# comando dado com GOOGLE_OAUTH_ACCESS_TOKEN apontando pra ele — nunca
# nenhum arquivo de credencial, nem temporário nem permanente. Usa a
# sessão pessoal do operador (`gcloud auth login`) para mintar o token via
# impersonação; exige que essa conta tenha sido concedida
# roles/iam.serviceAccountTokenCreator sobre a SA
# (create_terraform_service_account.sh já faz isso).
#
# Uso: infra/scripts/with_terraform_credentials.sh <project-id> -- <comando>
# Exemplo:
#   infra/scripts/with_terraform_credentials.sh meu-projeto -- \
#     docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
#     run --rm --entrypoint terraform tools -chdir=infra/bootstrap apply -var="project_id=meu-projeto"

set -euo pipefail

PROJECT_ID="${1:?uso: with_terraform_credentials.sh <project-id> -- <comando>}"
shift
if [[ "${1:-}" != "--" ]]; then
  echo "uso: with_terraform_credentials.sh <project-id> -- <comando>" >&2
  exit 1
fi
shift
if [[ $# -eq 0 ]]; then
  echo "uso: with_terraform_credentials.sh <project-id> -- <comando>" >&2
  exit 1
fi

SA_EMAIL="terraform-tcc@${PROJECT_ID}.iam.gserviceaccount.com"

GOOGLE_OAUTH_ACCESS_TOKEN="$(gcloud auth print-access-token --impersonate-service-account="$SA_EMAIL")" "$@"
