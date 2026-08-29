#!/bin/bash
# Cria a service account dedicada que o Terraform (dentro do container
# `tools`) usa para autenticar no GCP — Fase 4 (README.md). Roda no host
# (usa `gcloud` diretamente), não dentro do container `tools`, que não tem
# o gcloud CLI instalado.
#
# Segurança: nenhuma chave de longa duração é gerada. A política de
# organização desta conta (constraints/iam.disableServiceAccountKeyCreation,
# herdada, não configurável neste projeto) bloqueia
# `gcloud iam service-accounts keys create` de qualquer forma — o que
# acabou sendo a opção certa mesmo sem essa restrição: em vez de uma chave
# JSON, a conta pessoal do operador (quem roda este script) ganha
# `roles/iam.serviceAccountTokenCreator` sobre esta SA, e passa a poder
# IMPERSONÁ-LA sob demanda (tokens de acesso de curta duração, ~1h, nunca
# persistidos em disco) via infra/scripts/with_terraform_credentials.sh —
# nunca um arquivo de credencial de longa duração em lugar nenhum.
#
# Uso: infra/scripts/create_terraform_service_account.sh <project-id> <billing-account-id>
# Exemplo: infra/scripts/create_terraform_service_account.sh meu-projeto 012345-6789AB-CDEF01

set -euo pipefail

PROJECT_ID="${1:?uso: create_terraform_service_account.sh <project-id> <billing-account-id>}"
BILLING_ACCOUNT_ID="${2:?uso: create_terraform_service_account.sh <project-id> <billing-account-id>}"

SA_ID="terraform-tcc"
SA_EMAIL="${SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
OPERATOR="$(gcloud config get-value account 2>/dev/null)"

echo "Criando service account ${SA_EMAIL}..."
gcloud iam service-accounts create "$SA_ID" \
  --project="$PROJECT_ID" \
  --display-name="Terraform - TCC recsys retrieval" \
  || echo "  (já existe, seguindo em frente)"

# Papéis mínimos exigidos pelos recursos reais em infra/ — ver o plano de
# implementação para o mapeamento módulo -> papel.
#
# Os últimos quatro (cloudfunctions/run/eventarc/pubsub .admin) só existem
# por causa de infra/modules/budget_killswitch/ (Cloud Function Gen2 —
# builda sobre Cloud Run, dispara via Eventarc a partir de um tópico
# Pub/Sub). Esse conjunto é o recomendado pelo Google para quem faz o
# deploy de uma function Gen2, mas NÃO foi validado contra um
# `terraform apply` real ainda — se faltar algum papel, o próprio erro do
# apply aponta qual API está faltando.
ROLES=(
  "roles/compute.admin"
  "roles/storage.admin"
  "roles/iam.serviceAccountAdmin"
  "roles/iam.serviceAccountUser"
  "roles/resourcemanager.projectIamAdmin"
  "roles/cloudfunctions.admin"
  "roles/run.admin"
  "roles/eventarc.admin"
  "roles/pubsub.admin"
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

# Permite que a conta pessoal do operador (você, autenticado via
# `gcloud auth login`) impersone esta SA — é assim que o Terraform vai
# atuar como ela sem nenhuma chave existir em lugar nenhum.
echo "Concedendo iam.serviceAccountTokenCreator a ${OPERATOR} sobre ${SA_EMAIL}..."
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --project="$PROJECT_ID" \
  --member="user:${OPERATOR}" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --quiet

echo
echo "Pronto. Nenhuma chave foi criada — nenhum arquivo de credencial de"
echo "longa duração existe em lugar nenhum, nem local nem no Secret Manager."
echo
echo "Para rodar Terraform de verdade, use o wrapper que minta um token de"
echo "acesso por impersonação (válido por ~1h) só pela duração de cada comando:"
echo "  infra/scripts/with_terraform_credentials.sh $PROJECT_ID -- <comando docker compose>"
echo
echo "Quando o trabalho de campo terminar, revogue a impersonação:"
echo "  gcloud iam service-accounts remove-iam-policy-binding $SA_EMAIL \\"
echo "    --member=user:$OPERATOR --role=roles/iam.serviceAccountTokenCreator"
