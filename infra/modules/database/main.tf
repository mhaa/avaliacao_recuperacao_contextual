# VM do banco sob teste — Compute Engine rodando a MESMA imagem Docker
# usada localmente em docker-compose.yml (nunca um banco gerenciado —
# restrição explícita do projeto, ver CONTEXTO.md "Fora de escopo"). Um
# disco de dados separado do boot disk, para o snapshot pós-carga
# (infra/scripts/snapshot_after_load.sh) não incluir o SO.
#
# `storage` seleciona imagem/flags; `cell` só rotula recursos — os dois são
# passados separadamente (não parseados de "e1-postgres") porque
# cells/<id>.yaml já declara `storage` explicitamente; duplicar esse
# parsing aqui seria uma segunda fonte de verdade.

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
  description = "Id da célula (ex.: e1-postgres) — só rotula recursos."
}

variable "storage" {
  type        = string
  description = "Tecnologia de banco desta célula — seleciona imagem Docker e flags."
  validation {
    condition     = contains(["postgres", "valkey", "scylla", "opensearch"], var.storage)
    error_message = "storage precisa ser um de: postgres, valkey, scylla, opensearch."
  }
}

variable "subnetwork_self_link" {
  type        = string
  description = "Self-link da sub-rede privada (output do módulo network)."
}

variable "machine_type" {
  type        = string
  description = "Tipo de máquina — n2-standard-8 por padrão (IMPLEMENTACAO.md, topologia); dimensionamento.xlsx (Etapa 1) pode indicar outro valor por célula."
  default     = "n2-standard-8"
}

variable "data_disk_size_gb" {
  type        = number
  description = "Tamanho do disco de dados, em GB — snapshot tirado depois da carga."
  default     = 200
  validation {
    condition     = var.data_disk_size_gb > 0
    error_message = "data_disk_size_gb precisa ser positivo."
  }
}

# Mesma imagem/flags de docker-compose.yml — nada de configuração nova
# inventada aqui, só traduzida para `docker run` (evita confundir a
# comparação entre células com uma diferença acidental de configuração).
locals {
  docker_image = {
    postgres   = "postgres:16-alpine"
    valkey     = "valkey/valkey:8-alpine"
    scylla     = "scylladb/scylla:6.2"
    opensearch = "opensearchproject/opensearch:2.18.0"
  }
  docker_run_flags = {
    postgres   = "-p 5432:5432 -v /mnt/data:/var/lib/postgresql/data -e POSTGRES_USER=tcc -e POSTGRES_DB=recsys -c shared_buffers=1GB -c work_mem=64MB -c max_connections=200 -c random_page_cost=1.1 -c track_io_timing=on"
    valkey     = "-p 6379:6379 --save \"\" --appendonly no --maxmemory 24gb --maxmemory-policy noeviction"
    scylla     = "-p 9042:9042 -v /mnt/data:/var/lib/scylla --smp 1 --memory 2G --overprovisioned 1 --developer-mode 1 --skip-wait-for-gossip-to-settle 0"
    opensearch = "-p 9200:9200 -v /mnt/data:/usr/share/opensearch/data -e discovery.type=single-node -e bootstrap.memory_lock=true -e OPENSEARCH_JAVA_OPTS=-Xms2g -Xmx2g -e DISABLE_SECURITY_PLUGIN=true"
  }
}

resource "google_compute_disk" "data" {
  name = "tcc-${var.cell}-data"
  zone = var.zone
  size = var.data_disk_size_gb
  type = "pd-ssd"
}

resource "google_compute_instance" "database" {
  name         = "tcc-${var.cell}-database"
  zone         = var.zone
  machine_type = var.machine_type

  boot_disk {
    initialize_params {
      image = "cos-cloud/cos-stable"
    }
  }

  attached_disk {
    source      = google_compute_disk.data.id
    device_name = "data"
  }

  network_interface {
    subnetwork = var.subnetwork_self_link
    # Sem access_config: sem IP público (IMPLEMENTACAO.md).
  }

  metadata = {
    startup-script = <<-EOT
      #!/bin/bash
      set -euo pipefail
      mkdir -p /mnt/data
      mkfs.ext4 -F /dev/disk/by-id/google-data 2>/dev/null || true
      mount /dev/disk/by-id/google-data /mnt/data
      %{if var.storage == "postgres"}
      POSTGRES_PASSWORD=$(gcloud secrets versions access latest --secret=tcc-postgres-password)
      docker run -d --name tcc-database --restart unless-stopped \
        -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
        ${local.docker_run_flags["postgres"]} \
        ${local.docker_image["postgres"]}
      %{else}
      docker run -d --name tcc-database --restart unless-stopped \
        ${local.docker_run_flags[var.storage]} \
        ${local.docker_image[var.storage]}
      %{endif}
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
  description = "IP interno da VM de banco — consumido pelo módulo service."
  value       = google_compute_instance.database.network_interface[0].network_ip
}

output "data_disk_name" {
  description = "Nome do disco de dados — usado por infra/scripts/snapshot_after_load.sh."
  value       = google_compute_disk.data.name
}
