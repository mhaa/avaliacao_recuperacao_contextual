# infra/bootstrap — a única exceção documentada a "nunca estado local"
# (IMPLEMENTACAO.md, "Backend remoto com bloqueio de estado"): o bucket de
# estado remoto precisa existir ANTES de qualquer `terraform init
# -backend-config` em infra/envs/* poder apontar para ele. Rodado uma vez,
# manualmente, com autenticação real — nunca por CI.
#
# Uso:
#   cd infra/bootstrap
#   terraform init
#   terraform validate
#   terraform apply -var="project_id=<seu-projeto>"   # cria recursos reais — confirmar antes

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
}

variable "project_id" {
  type        = string
  description = "Projeto GCP onde os buckets de estado e de resultados são criados."
  validation {
    condition     = length(var.project_id) > 0
    error_message = "project_id não pode ser vazio."
  }
}

variable "region" {
  type        = string
  description = "Região única do experimento (CONTEXTO.md: \"Nuvem... região única\")."
  default     = "us-central1"
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# Bucket de estado remoto do Terraform — SEM prevent_destroy
# (IMPLEMENTACAO.md: "prevent_destroy no bucket de resultados. No resto, não.").
resource "google_storage_bucket" "terraform_state" {
  name                        = "${var.project_id}-tcc-tfstate"
  location                    = var.region
  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }

  labels = {
    project    = "tcc-recsys-retrieval"
    phase      = "bootstrap"
    managed_by = "terraform"
  }
}

# Bucket de resultados — durável além do ciclo de vida das VMs por célula
# (analysis/collect.py sobe os 5 arquivos de results/<cell>/<phase>/<timestamp>/
# aqui quando rodar na nuvem). prevent_destroy explícito, única exceção do
# projeto (IMPLEMENTACAO.md, "Custo").
resource "google_storage_bucket" "results" {
  name                        = "${var.project_id}-tcc-results"
  location                    = var.region
  uniform_bucket_level_access = true

  versioning {
    enabled = true
  }

  labels = {
    project    = "tcc-recsys-retrieval"
    phase      = "bootstrap"
    managed_by = "terraform"
  }

  lifecycle {
    prevent_destroy = true
  }
}

output "terraform_state_bucket" {
  description = "Nome do bucket a passar em -backend-config=\"bucket=...\" nos envs/*."
  value       = google_storage_bucket.terraform_state.name
}

output "results_bucket" {
  description = "Nome do bucket onde os resultados coletados (results/) devem ser enviados."
  value       = google_storage_bucket.results.name
}
