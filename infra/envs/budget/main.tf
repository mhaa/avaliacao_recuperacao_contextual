# Alerta de orçamento — aplicação separada, rodada uma vez ANTES de
# qualquer célula (IMPLEMENTACAO.md: "google_billing_budget não depende
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
  }
  backend "gcs" {
    # bucket via -backend-config; prefix fixo "budget" (não varia por célula).
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
  description = "Limite mensal em USD — ajustar conforme dimensionamento.xlsx (Etapa 1)."
  default     = 200
  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "monthly_budget_usd precisa ser positivo."
  }
}

provider "google-beta" {
  project = var.project_id
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
      currency_code = "USD"
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
}
