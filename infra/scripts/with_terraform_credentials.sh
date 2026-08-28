#!/bin/bash
# Busca a chave da service account do Terraform no Secret Manager
# (segredo "tcc-terraform-key", criado por
# infra/scripts/create_terraform_service_account.sh) para um arquivo
# temporário, roda o comando dado com GCP_TERRAFORM_KEY_PATH apontando
# pra ele, e apaga o arquivo depois — mesmo se o comando falhar. Usa a
# sessão pessoal do operador (`gcloud auth login`) pra ler o secret, não a
# própria service account do Terraform (seria circular: ela não pode ler a
# própria chave para se autenticar).
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

TMP_KEY="$(mktemp)"
trap 'rm -f "$TMP_KEY"' EXIT

gcloud secrets versions access latest --secret=tcc-terraform-key --project="$PROJECT_ID" > "$TMP_KEY"

GCP_TERRAFORM_KEY_PATH="$TMP_KEY" "$@"
