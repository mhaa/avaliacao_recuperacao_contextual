#!/bin/bash
# Cria a service account dedicada que o Terraform (dentro do container
# `tools`) usa para autenticar no GCP — Fase 4 (README.md). Roda no host
# (usa `gcloud` diretamente), não dentro do container `tools`, que não tem
# o gcloud CLI instalado.
#
# Segurança (decisão explícita do usuário: credenciais nunca vão para o
# GitHub, e nem devem persistir indefinidamente em disco local): a chave
# gerada é guardada no Secret Manager (segredo "tcc-terraform-key") e a
# cópia local temporária usada só pra fazer o upload é apagada logo em
# seguida. Rodar Terraform de verdade depois exige buscar essa chave de
# novo, sob demanda, via infra/scripts/with_terraform_credentials.sh — que
# a materializa num arquivo temporário só pela duração de um comando e
# apaga depois. Nunca fica um arquivo de chave permanente em lugar nenhum.
#
# Uso: infra/scripts/create_terraform_service_account.sh <project-id> <billing-account-id>
# Exemplo: infra/scripts/create_terraform_service_account.sh meu-projeto 012345-6789AB-CDEF01

set -euo pipefail

PROJECT_ID="${1:?uso: create_terraform_service_account.sh <project-id> <billing-account-id>}"
BILLING_ACCOUNT_ID="${2:?uso: create_terraform_service_account.sh <project-id> <billing-account-id>}"

SA_ID="terraform-tcc"
SA_EMAIL="${SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"
SECRET_ID="tcc-terraform-key"

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

# Arquivo temporário só para o upload — nunca o destino final da chave.
# `mktemp` nunca resolve dentro deste repositório, então não precisa de
# checagem extra (ao contrário da versão anterior deste script).
TMP_KEY="$(mktemp)"
trap 'rm -f "$TMP_KEY"' EXIT

echo "Gerando chave..."
gcloud iam service-accounts keys create "$TMP_KEY" \
  --iam-account="$SA_EMAIL"

echo "Guardando a chave no Secret Manager (segredo '${SECRET_ID}')..."
if gcloud secrets describe "$SECRET_ID" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud secrets versions add "$SECRET_ID" \
    --project="$PROJECT_ID" \
    --data-file="$TMP_KEY"
else
  gcloud secrets create "$SECRET_ID" \
    --project="$PROJECT_ID" \
    --replication-policy=automatic \
    --data-file="$TMP_KEY"
fi

echo
echo "Pronto. A chave está no Secret Manager, não em nenhum arquivo local"
echo "(o temporário usado pra subir foi apagado)."
echo
echo "Para rodar Terraform de verdade, use o wrapper que busca a chave sob"
echo "demanda e apaga depois de cada comando:"
echo "  infra/scripts/with_terraform_credentials.sh $PROJECT_ID -- <comando docker compose>"
echo
echo "Quando o trabalho de campo terminar, revogue a chave e delete o secret:"
echo "  gcloud iam service-accounts keys list --iam-account=$SA_EMAIL"
echo "  gcloud iam service-accounts keys delete <KEY_ID> --iam-account=$SA_EMAIL"
echo "  gcloud secrets delete $SECRET_ID --project=$PROJECT_ID"
