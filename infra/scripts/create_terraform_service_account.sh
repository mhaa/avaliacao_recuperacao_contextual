#!/bin/bash
# Cria a service account dedicada que o Terraform (dentro do container
# `tools`) usa para autenticar no GCP — Fase 4 (README.md). Roda no host
# (usa `gcloud` diretamente), não dentro do container `tools`, que não tem
# o gcloud CLI instalado.
#
# Segurança (decisão explícita do usuário: credenciais nunca vão para o
# GitHub): este script SE RECUSA a escrever a chave dentro da árvore deste
# repositório Git. A chave deve viver em outro lugar do disco — depois de
# gerada, exporte GCP_TERRAFORM_KEY_PATH apontando pra ela e use
# docker-compose.gcp.yml para montá-la no container só quando for rodar
# Terraform de verdade.
#
# Uso: infra/scripts/create_terraform_service_account.sh <project-id> <billing-account-id> <output-key-path>
# Exemplo: infra/scripts/create_terraform_service_account.sh meu-projeto 012345-6789AB-CDEF01 "$HOME/.tcc-secrets/terraform-key.json"

set -euo pipefail

PROJECT_ID="${1:?uso: create_terraform_service_account.sh <project-id> <billing-account-id> <output-key-path>}"
BILLING_ACCOUNT_ID="${2:?uso: create_terraform_service_account.sh <project-id> <billing-account-id> <output-key-path>}"
OUTPUT_KEY_PATH="${3:?uso: create_terraform_service_account.sh <project-id> <billing-account-id> <output-key-path>}"

# --- Checagem de segurança: a chave nunca pode cair dentro do repo. ---
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CANDIDATE="$(dirname "$OUTPUT_KEY_PATH")"
while [[ ! -d "$CANDIDATE" && "$CANDIDATE" != "/" && "$CANDIDATE" != "." ]]; do
  CANDIDATE="$(dirname "$CANDIDATE")"
done
OUTPUT_DIR="$(cd "$CANDIDATE" && pwd)"
case "$OUTPUT_DIR" in
  "$REPO_ROOT"|"$REPO_ROOT"/*)
    echo "ERRO: <output-key-path> ('$OUTPUT_KEY_PATH') cai dentro do repositório ('$REPO_ROOT')." >&2
    echo "A chave da service account NUNCA pode ficar numa pasta versionada, mesmo com .gitignore." >&2
    echo "Escolha um caminho fora do repo, ex.: \$HOME/.tcc-secrets/terraform-key.json" >&2
    exit 1
    ;;
esac

SA_ID="terraform-tcc"
SA_EMAIL="${SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "Criando service account ${SA_EMAIL}..."
gcloud iam service-accounts create "$SA_ID" \
  --project="$PROJECT_ID" \
  --display-name="Terraform - TCC recsys retrieval" \
  || echo "  (já existe, seguindo em frente)"

# Papéis mínimos exigidos pelos recursos reais em infra/ — ver o plano de
# implementação para o mapeamento módulo -> papel.
ROLES=(
  "roles/compute.admin"
  "roles/storage.admin"
  "roles/iam.serviceAccountAdmin"
  "roles/iam.serviceAccountUser"
  "roles/resourcemanager.projectIamAdmin"
)
for ROLE in "${ROLES[@]}"; do
  echo "Concedendo ${ROLE} no projeto ${PROJECT_ID}..."
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="$ROLE" \
    --condition=None \
    --quiet
done

# google_billing_budget (infra/envs/budget) exige um papel na CONTA DE
# FATURAMENTO, não no projeto. roles/billing.admin é o mais amplo papel
# predefinido estável para isso — ajuste se o gcloud recusar por política
# da sua organização.
echo "Concedendo roles/billing.admin na conta de faturamento ${BILLING_ACCOUNT_ID}..."
gcloud billing accounts add-iam-policy-binding "$BILLING_ACCOUNT_ID" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/billing.admin" \
  --quiet

mkdir -p "$OUTPUT_DIR"
echo "Gerando chave em ${OUTPUT_KEY_PATH}..."
gcloud iam service-accounts keys create "$OUTPUT_KEY_PATH" \
  --iam-account="$SA_EMAIL"

echo
echo "Pronto. Antes de rodar Terraform via docker-compose.gcp.yml:"
echo "  export GCP_TERRAFORM_KEY_PATH=\"$OUTPUT_KEY_PATH\""
echo
echo "Trate '$OUTPUT_KEY_PATH' como um segredo: nunca copie para dentro do"
echo "repositório, nunca cole em chat/issue. Quando o trabalho de campo"
echo "terminar, revogue com:"
echo "  gcloud iam service-accounts keys delete <KEY_ID> --iam-account=$SA_EMAIL"
