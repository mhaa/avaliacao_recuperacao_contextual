# Cloud Function que reage ao mesmo tópico Pub/Sub do orçamento
# (infra/envs/budget/main.tf: all_updates_rule aponta pra cá) como trava
# de segurança — quando o gasto atinge KILL_THRESHOLD (function_src/main.py,
# hoje 1.2, em sincronia com o threshold_rules de 1.2 do orçamento),
# desliga o billing do projeto inteiro (projects.updateBillingInfo), não
# só as VMs que o Terraform conhece.
#
# Ferramenta auxiliar de operação — nunca participa do caminho medido
# (README.md, Fase 4: "Compute Engine, não Cloud Run" é sobre a camada de
# serviço sob teste, não sobre esta trava de segurança).
#
# killswitch_dry_run tem default true de propósito: um `terraform apply`
# deste módulo nunca arma, por acidente, uma function que já corta billing
# de verdade — virar `false` é uma decisão explícita e separada (ver
# README.md, seção da trava de segurança).

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

variable "project_id" {
  type        = string
  description = "Projeto GCP."
}

variable "region" {
  type        = string
  description = "Região onde a function é criada."
  default     = "us-east4"
}

variable "function_source_bucket" {
  type        = string
  description = "Bucket para o zip do código-fonte da function (output do bootstrap: function_source_bucket)."
}

variable "killswitch_dry_run" {
  type        = bool
  description = "Se true (default), a function só loga o que faria — nunca desliga o billing de verdade."
  default     = true
}

resource "google_pubsub_topic" "budget_alerts" {
  project = var.project_id
  name    = "tcc-budget-alerts"

  labels = {
    project    = "tcc-recsys-retrieval"
    phase      = "budget"
    managed_by = "terraform"
  }
}

resource "google_service_account" "killswitch" {
  project      = var.project_id
  account_id   = "tcc-budget-killswitch"
  display_name = "tcc-recsys budget killswitch (Cloud Function)"
}

# Papel mínimo documentado pelo Google para permitir
# projects.updateBillingInfo sem dar roles/billing.admin completo à SA de
# runtime da function. CONFIRMADO ao aplicar de verdade pela primeira
# vez: sozinho não basta — só inclui
# resourcemanager.projects.{create,delete}BillingAssignment
# (gcloud iam roles describe roles/billing.projectManager), nenhuma
# permissão de LEITURA. get_project_billing_info() (chamado ANTES do
# updateBillingInfo, pra não repetir a chamada se o billing já estiver
# desligado) exige resourcemanager.projects.get, que só apareceu numa
# invocação real como "403 PermissionDenied: The caller does not have
# permission" — nunca detectável sem essa invocação de verdade.
resource "google_project_iam_member" "killswitch_billing_manager" {
  project = var.project_id
  role    = "roles/billing.projectManager"
  member  = "serviceAccount:${google_service_account.killswitch.email}"
}

# roles/browser: só resourcemanager.projects.{get,list} (+ folders/orgs
# equivalentes) — nenhum acesso a custo/pagamento da conta de billing,
# ao contrário de roles/billing.admin, que teria resolvido isso mas é
# exatamente o que o comentário acima diz pra evitar.
resource "google_project_iam_member" "killswitch_project_viewer" {
  project = var.project_id
  role    = "roles/browser"
  member  = "serviceAccount:${google_service_account.killswitch.email}"
}

# Lista explícita de arquivos (não source_dir) para o zip nunca incluir
# test_main.py, que fica ao lado de main.py só para o pytest local achar.
data "archive_file" "function_zip" {
  type        = "zip"
  output_path = "${path.module}/function_src.zip"

  source {
    content  = file("${path.module}/function_src/main.py")
    filename = "main.py"
  }
  source {
    content  = file("${path.module}/function_src/requirements.txt")
    filename = "requirements.txt"
  }
}

resource "google_storage_bucket_object" "function_zip" {
  name   = "budget-killswitch/${data.archive_file.function_zip.output_md5}.zip"
  bucket = var.function_source_bucket
  source = data.archive_file.function_zip.output_path
}

resource "google_cloudfunctions2_function" "budget_killswitch" {
  project  = var.project_id
  name     = "tcc-budget-killswitch"
  location = var.region

  build_config {
    runtime     = "python312"
    entry_point = "handle_budget_notification"
    source {
      storage_source {
        bucket = var.function_source_bucket
        object = google_storage_bucket_object.function_zip.name
      }
    }
  }

  service_config {
    max_instance_count    = 1
    min_instance_count    = 0
    available_memory      = "256M"
    timeout_seconds       = 60
    service_account_email = google_service_account.killswitch.email
    environment_variables = {
      DRY_RUN = tostring(var.killswitch_dry_run)
      # Gen2 (Cloud Run por baixo) NÃO injeta isso automaticamente — só
      # Gen1 fazia. Sem isso, main.py's os.environ["GOOGLE_CLOUD_PROJECT"]
      # explode com KeyError (confirmado numa invocação real, a primeira
      # a passar da barreira de IAM).
      GOOGLE_CLOUD_PROJECT = var.project_id
    }
  }

  event_trigger {
    trigger_region = var.region
    event_type     = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic   = google_pubsub_topic.budget_alerts.id
    retry_policy   = "RETRY_POLICY_DO_NOT_RETRY"
    # Sem isso, o gatilho usa a SA de compute padrão do projeto para
    # invocar a function — e essa organização desabilita concessões
    # automáticas de IAM para SAs padrão (mesma causa raiz do bug do
    # Cloud Build na Fase 4 passo 3), então a chamada falha com "The
    # IAM principal lacks {run.routes.invoke} permission" (confirmado
    # publicando uma notificação sintética de verdade no tópico: zero
    # execuções da function, só esse erro nos logs do Cloud Run).
    service_account_email = google_service_account.killswitch.email
  }

  labels = {
    project    = "tcc-recsys-retrieval"
    phase      = "budget"
    managed_by = "terraform"
  }
}

# Function Gen2 roda sobre um serviço Cloud Run por baixo — a
# subscription push do Pub/Sub (event_trigger acima) precisa de
# roles/run.invoker NESSE serviço para efetivamente conseguir chamá-lo,
# além do service_account_email do próprio gatilho já apontar pra essa
# SA. Reaproveita a SA de runtime da function (google_service_account.
# killswitch) em vez de criar uma segunda SA só para isso.
resource "google_cloud_run_service_iam_member" "killswitch_invoker" {
  project  = var.project_id
  location = var.region
  service  = google_cloudfunctions2_function.budget_killswitch.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.killswitch.email}"
}

output "topic_id" {
  description = "Id do tópico Pub/Sub — usado pelo all_updates_rule do google_billing_budget em envs/budget/main.tf."
  value       = google_pubsub_topic.budget_alerts.id
}

output "function_name" {
  description = "Nome da Cloud Function — para inspecionar logs (gcloud functions logs read)."
  value       = google_cloudfunctions2_function.budget_killswitch.name
}
