# Aplicação por célula — um `terraform init -backend-config` + `apply` por
# célula, com prefixo de estado isolado (IMPLEMENTACAO.md, "Isolamento por
# célula": "prefixo de estado separado por célula... em vez de workspaces
# — mais fácil de auditar, menor risco de `terraform workspace select`
# errado destruir a célula errada").
#
# Uso (só depois que infra/bootstrap existir e o usuário tiver
# project_id/billing_account_id reais — NENHUM plan/apply real nesta fase):
#   cd infra/envs/experiment
#   cp terraform.tfvars.example terraform.tfvars   # preencher, nunca commitar
#   terraform init \
#     -backend-config="bucket=<saída de bootstrap: terraform_state_bucket>" \
#     -backend-config="prefix=cells/${cell}"
#   terraform validate
#   terraform plan      # NUNCA apply sem confirmação explícita e separada — recursos faturáveis

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
  backend "gcs" {
    # bucket e prefix vêm de -backend-config na linha de comando — nunca
    # hardcoded aqui, ou todas as células compartilhariam o mesmo estado.
  }
}

variable "project_id" {
  type        = string
  description = "Projeto GCP do experimento."
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

variable "zone" {
  type        = string
  description = "Zona dentro da região."
  default     = "us-central1-a"
}

variable "cell" {
  type        = string
  description = "Id da célula sendo medida — ver cells/*.yaml."
  validation {
    condition = contains([
      "e1-postgres", "e2-postgres", "e3-postgres", "e4-postgres",
      "e1-valkey", "e2-valkey", "e3-valkey", "e4-valkey",
      "e1-scylla", "e2-scylla", "e3-scylla",
      "e1-opensearch", "e2-opensearch", "e4-opensearch",
    ], var.cell)
    error_message = "cell precisa ser uma das 14 células viáveis em cells/*.yaml."
  }
}

variable "storage" {
  type        = string
  description = "Tecnologia de banco da célula — deve bater com o campo `storage` de cells/<cell>.yaml."
  validation {
    condition     = contains(["postgres", "valkey", "scylla", "opensearch"], var.storage)
    error_message = "storage precisa ser um de: postgres, valkey, scylla, opensearch."
  }
}

variable "service_image" {
  type        = string
  description = "Imagem do service/ (docker/Dockerfile.service) em Artifact Registry."
}

variable "tools_image" {
  type        = string
  description = "Imagem do tools/ (docker/Dockerfile.tools) em Artifact Registry, usada pela VM de loadgen."
}

variable "dataset_bucket" {
  type        = string
  description = "Bucket com a massa de dados completa (output do bootstrap: dataset_bucket)."
}

provider "google" {
  project = var.project_id
  region  = var.region
}

module "network" {
  source     = "../../modules/network"
  project_id = var.project_id
  region     = var.region
}

module "database" {
  source               = "../../modules/database"
  project_id           = var.project_id
  zone                 = var.zone
  cell                 = var.cell
  storage              = var.storage
  subnetwork_self_link = module.network.subnetwork_self_link
}

module "service" {
  source               = "../../modules/service"
  project_id           = var.project_id
  zone                 = var.zone
  cell                 = var.cell
  subnetwork_self_link = module.network.subnetwork_self_link
  service_image        = var.service_image
  database_internal_ip = module.database.internal_ip
}

module "loadgen" {
  source               = "../../modules/loadgen"
  project_id           = var.project_id
  zone                 = var.zone
  cell                 = var.cell
  subnetwork_self_link = module.network.subnetwork_self_link
  tools_image          = var.tools_image
  dataset_bucket       = var.dataset_bucket
}

output "database_internal_ip" {
  description = "IP interno da VM de banco desta célula."
  value       = module.database.internal_ip
}

output "service_internal_ip" {
  description = "IP interno da VM de serviço — TARGET_URL para load/scenarios.js."
  value       = module.service.internal_ip
}

output "loadgen_internal_ip" {
  description = "IP interno da VM do gerador de carga."
  value       = module.loadgen.internal_ip
}

output "data_disk_name" {
  description = "Nome do disco de dados — para infra/scripts/snapshot_after_load.sh."
  value       = module.database.data_disk_name
}
