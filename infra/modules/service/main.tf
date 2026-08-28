# VM do serviço de recuperação (service/) — roda a imagem de
# docker/Dockerfile.service, publicada em Artifact Registry fora deste
# módulo (build/push é responsabilidade de CI/CD, não do Terraform). O
# startup script busca credenciais do Secret Manager antes do `docker run`
# — decisão já tomada na Etapa 6 (core/registry.py): infra/, não código
# Python, é responsável por isso, mantendo a imagem do serviço sem
# nenhuma dependência de GCP.

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
  description = "Projeto GCP."
}

variable "zone" {
  type        = string
  description = "Zona onde a VM é criada."
}

variable "cell" {
  type        = string
  description = "Id da célula — rotula recursos e vira a env var CELL do container (core/registry.py)."
}

variable "subnetwork_self_link" {
  type        = string
  description = "Self-link da sub-rede privada (output do módulo network)."
}

variable "service_image" {
  type        = string
  description = "Referência completa da imagem em Artifact Registry (ex.: us-central1-docker.pkg.dev/PROJECT/tcc/service:TAG)."
}

variable "database_internal_ip" {
  type        = string
  description = "IP interno da VM de banco desta célula (output do módulo database)."
}

variable "machine_type" {
  type        = string
  description = "Tipo de máquina — n2-standard-4 (IMPLEMENTACAO.md, topologia)."
  default     = "n2-standard-4"
}

resource "google_service_account" "service" {
  account_id   = "tcc-${var.cell}-service"
  display_name = "tcc-recsys service account (${var.cell})"
}

# Só o necessário: ler segredos do Secret Manager, nada mais.
resource "google_project_iam_member" "service_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.service.email}"
}

resource "google_compute_instance" "service" {
  name         = "tcc-${var.cell}-service"
  zone         = var.zone
  machine_type = var.machine_type

  boot_disk {
    initialize_params {
      image = "cos-cloud/cos-stable"
    }
  }

  network_interface {
    subnetwork = var.subnetwork_self_link
    # Sem access_config: sem IP público (IMPLEMENTACAO.md).
  }

  service_account {
    email  = google_service_account.service.email
    scopes = ["cloud-platform"]
  }

  metadata = {
    startup-script = <<-EOT
      #!/bin/bash
      set -euo pipefail
      POSTGRES_PASSWORD=$(gcloud secrets versions access latest --secret=tcc-postgres-password || echo "")
      docker run -d --name tcc-service --restart unless-stopped \
        -e CELL=${var.cell} \
        -e STORAGE_HOST=${var.database_internal_ip} \
        -e POSTGRES_USER=tcc -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" -e POSTGRES_DB=recsys \
        -p 8000:8000 -p 50051:50051 \
        ${var.service_image}
    EOT
  }

  labels = {
    project    = "tcc-recsys-retrieval"
    cell       = var.cell
    phase      = "experiment"
    managed_by = "terraform"
  }
}

output "internal_ip" {
  description = "IP interno da VM de serviço — usado como TARGET_URL pelo módulo loadgen/load/scenarios.js."
  value       = google_compute_instance.service.network_interface[0].network_ip
}
