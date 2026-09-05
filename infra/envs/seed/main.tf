# Root mínimo pra semear o dataset completo uma vez por tecnologia de
# banco (infra/scripts/seed_dataset_snapshots.py) — réplica enxuta de
# ../experiment: só network + database + loadgen, sem service (semear não
# precisa do serviço de recuperação, só do banco e de uma VM com a imagem
# `tools` pronta pra rodar o schema+carga via SSH).
#
# `cell` do módulo database/loadgen é só rótulo de recursos
# ("seed-<storage>", nunca colide com as 14 células reais de
# cells/*.yaml — o módulo database não valida esse campo). Sem
# data_disk_snapshot aqui: semear sempre parte de disco em branco (é o que
# está sendo criado); o default "" do módulo já cobre isso.
#
# Uso (mesma disciplina de ../experiment — nunca commitar terraform.tfvars):
#   cd infra/envs/seed
#   cp terraform.tfvars.example terraform.tfvars
#   terraform init \
#     -backend-config="bucket=<saída de bootstrap: terraform_state_bucket>" \
#     -backend-config="prefix=seed/${storage}"
#   terraform plan
#   terraform apply   # NUNCA sem confirmação explícita e separada — recursos faturáveis

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
  backend "gcs" {
    # bucket e prefix vêm de -backend-config na linha de comando — mesmo
    # motivo de ../experiment (prefixo isolado por tecnologia de banco).
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
  description = "Região única do experimento (mesma de ../experiment)."
  default     = "us-central1"
}

variable "zone" {
  type        = string
  description = "Zona dentro da região."
  default     = "us-central1-a"
}

variable "storage" {
  type        = string
  description = "Tecnologia de banco sendo semeada — nunca valkey (sem disco persistente, ver README.md)."
  validation {
    condition     = contains(["postgres", "scylla", "opensearch"], var.storage)
    error_message = "storage precisa ser um de: postgres, scylla, opensearch (valkey não usa disco persistente)."
  }
}

variable "tools_image" {
  type        = string
  description = "Imagem do tools/ (docker/Dockerfile.tools) em Artifact Registry, usada pela VM de loadgen pra rodar o schema+carga."
}

variable "dataset_bucket" {
  type        = string
  description = "Bucket com a massa de dados completa (output do bootstrap: dataset_bucket)."
}

variable "results_bucket" {
  type        = string
  description = "Bucket de resultados (output do bootstrap: results_bucket) — semear não sobe resultado nenhum, mas module.loadgen exige o valor pra criar a IAM binding de escrita, mesmo motivo de dataset_bucket acima."
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  seed_cell = "seed-${var.storage}"
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
  cell                 = local.seed_cell
  storage              = var.storage
  subnetwork_self_link = module.network.subnetwork_self_link
}

module "loadgen" {
  source               = "../../modules/loadgen"
  project_id           = var.project_id
  zone                 = var.zone
  cell                 = local.seed_cell
  subnetwork_self_link = module.network.subnetwork_self_link
  tools_image          = var.tools_image
  dataset_bucket       = var.dataset_bucket
  results_bucket       = var.results_bucket
}

output "database_internal_ip" {
  description = "IP interno da VM de banco a ser semeada."
  value       = module.database.internal_ip
}

output "database_instance_id" {
  description = "ID numérico da VM de banco — filtro do Cloud Monitoring, se precisar depurar."
  value       = module.database.instance_id
}

output "data_disk_name" {
  description = "Nome do disco de dados — o que infra/scripts/seed_dataset_snapshots.py snapshota."
  value       = module.database.data_disk_name
}

output "loadgen_internal_ip" {
  description = "IP interno da VM de loadgen — não usado diretamente, só documentado por simetria com ../experiment."
  value       = module.loadgen.internal_ip
}
