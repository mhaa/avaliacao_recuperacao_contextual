# Alerta de orçamento — aplicação separada, rodada uma vez ANTES de
# qualquer célula (docs/ARCHITECTURE.md: "google_billing_budget não depende
# estruturalmente de VM nenhuma... ordem procedural, não uma dependência
# de grafo do Terraform"; e "Alerta de orçamento configurado ANTES da
# primeira VM").

terraform {
  required_version = ">= 1.5"
  required_providers {
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 5.0"
    }
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
  backend "gcs" {
    # bucket via -backend-config (varia por projeto); prefix fixo aqui —
    # ao contrário de envs/experiment, este env não varia por célula.
    prefix = "budget"
  }
}

variable "project_id" {
  type        = string
  description = "Projeto GCP monitorado pelo orçamento."
  validation {
    condition     = length(var.project_id) > 0
    error_message = "project_id não pode ser vazio."
  }
}

variable "billing_account_id" {
  type        = string
  description = "Conta de faturamento do projeto GCP."
}

variable "monthly_budget_usd" {
  type        = number
  description = "Limite mensal, na moeda de var.currency_code (nome da variável é histórico — ajustar conforme dimensionamento.xlsx (Etapa 1))."
  default     = 200
  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "monthly_budget_usd precisa ser positivo."
  }
}

variable "currency_code" {
  type        = string
  description = "Código ISO 4217 da moeda do orçamento — PRECISA bater com a moeda da conta de faturamento (gcloud billing accounts describe <id>), senão a criação falha com \"Error 400: Request contains an invalid argument\" (descoberto na prática: não é bug do provider nem da impersonação, é mismatch de moeda)."
  default     = "USD"
}

variable "region" {
  type        = string
  description = "Região da Cloud Function da trava de segurança (module.budget_killswitch)."
  default     = "us-east4"
}

variable "function_source_bucket" {
  type        = string
  description = "Bucket para o zip da Cloud Function da trava — output do bootstrap: function_source_bucket."
}

variable "killswitch_dry_run" {
  type        = bool
  description = "Repassado a module.budget_killswitch — true (default) até a trava ser validada de propósito (ver README.md)."
  default     = true
}

provider "google-beta" {
  project = var.project_id
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Trava de segurança: Cloud Function acionada pelo tópico Pub/Sub abaixo,
# desliga o billing do projeto se o gasto cruzar 120% (threshold_rules
# abaixo). Ver infra/modules/budget_killswitch/main.tf.
module "budget_killswitch" {
  source                 = "../../modules/budget_killswitch"
  project_id             = var.project_id
  region                 = var.region
  function_source_bucket = var.function_source_bucket
  killswitch_dry_run     = var.killswitch_dry_run
}

resource "google_billing_budget" "experiment" {
  provider        = google-beta
  billing_account = var.billing_account_id
  display_name    = "tcc-recsys-retrieval"

  budget_filter {
    projects = ["projects/${var.project_id}"]
  }

  amount {
    specified_amount {
      currency_code = var.currency_code
      units         = tostring(var.monthly_budget_usd)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }
  # Limiar dedicado da trava de segurança — separado do alerta
  # informativo de 100% acima. CURRENT_SPEND (não FORECASTED_SPEND): a
  # trava só deve agir sobre gasto que já aconteceu, não sobre projeção.
  threshold_rules {
    threshold_percent = 1.2
    spend_basis       = "CURRENT_SPEND"
  }

  # Publica toda mudança de threshold no tópico do module.budget_killswitch
  # — a function decide sozinha (function_src/main.py: KILL_THRESHOLD) se
  # a notificação corresponde ao limiar de 120% ou é só um dos alertas de
  # 50/90/100% informativos. disable_default_iam_recipients não é setado
  # (fica false): o e-mail de alerta padrão continua chegando também.
  all_updates_rule {
    pubsub_topic = module.budget_killswitch.topic_id
  }
}
