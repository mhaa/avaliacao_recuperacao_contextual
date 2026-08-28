# VM do gerador de carga — obrigatoriamente separada da VM de serviço
# (IMPLEMENTACAO.md: "o gerador em VM separada não é detalhe: a
# metodologia exige verificar que a CPU do gerador ficou abaixo de 60%").
# Mesma imagem `tools` usada localmente (docker/Dockerfile.tools), que já
# carrega o binário do k6 e load/scenarios.js (Etapa 7).

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
  description = "Id da célula — rotula recursos."
}

variable "subnetwork_self_link" {
  type        = string
  description = "Self-link da sub-rede privada (output do módulo network)."
}

variable "tools_image" {
  type        = string
  description = "Referência completa da imagem `tools` (docker/Dockerfile.tools) em Artifact Registry."
}

variable "machine_type" {
  type        = string
  description = "Tipo de máquina — n2-standard-8 (IMPLEMENTACAO.md, topologia)."
  default     = "n2-standard-8"
}

resource "google_compute_instance" "loadgen" {
  name         = "tcc-${var.cell}-loadgen"
  zone         = var.zone
  machine_type = var.machine_type

  boot_disk {
    initialize_params {
      image = "cos-cloud/cos-stable"
    }
  }

  network_interface {
    subnetwork = var.subnetwork_self_link
    # Sem IP público — alcançável só via IAP; load/run_battery.py roda
    # daqui dentro, não da estação do operador.
  }

  metadata = {
    startup-script = <<-EOT
      #!/bin/bash
      set -euo pipefail
      docker pull ${var.tools_image}
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
  description = "IP interno da VM do gerador de carga."
  value       = google_compute_instance.loadgen.network_interface[0].network_ip
}
