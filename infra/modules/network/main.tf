# Rede privada única para banco, serviço e gerador de carga. Sem IP público
# em nenhuma VM (docs/ARCHITECTURE.md, "Sem IP público nas VMs de banco e
# serviço. Acesso por IAP ou bastion." — aplicado também ao gerador aqui,
# por consistência: ele também não precisa ser alcançável de fora). Cloud
# NAT dá saída à internet (pull de imagem Docker, apt) sem IP público em
# nenhuma instância.

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

variable "region" {
  type        = string
  description = "Região única do experimento."
}

variable "subnet_cidr" {
  type        = string
  description = "CIDR IPv4 da sub-rede privada."
  default     = "10.0.0.0/24"
  validation {
    condition     = can(cidrhost(var.subnet_cidr, 0))
    error_message = "subnet_cidr precisa ser um bloco CIDR IPv4 válido."
  }
}

variable "name_suffix" {
  type        = string
  description = <<-EOT
    Sufixo dos nomes de recurso; vazio por padrão (renderização byte a byte
    idêntica à atual). Ver a mesma variável em infra/modules/service/main.tf.

    Aqui o sufixo importa MAIS que nos outros módulos: estes nomes são fixos,
    não derivados de `var.cell`, então dois estados Terraform sem sufixo
    disputariam literalmente a mesma VPC, sub-rede, regras de firewall,
    router e NAT.
  EOT
  default     = ""

  validation {
    condition     = length(var.name_suffix) <= 4
    error_message = "name_suffix precisa ter no máximo 4 caracteres."
  }
}

resource "google_compute_network" "main" {
  name                    = "tcc-recsys-network${var.name_suffix}"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "private" {
  name                     = "tcc-recsys-subnet${var.name_suffix}"
  ip_cidr_range            = var.subnet_cidr
  region                   = var.region
  network                  = google_compute_network.main.id
  private_ip_google_access = true
}

# SSH só via faixa de IP do IAP (docs/ARCHITECTURE.md) — nunca 0.0.0.0/0.
resource "google_compute_firewall" "allow_iap_ssh" {
  name          = "tcc-recsys-allow-iap-ssh${var.name_suffix}"
  network       = google_compute_network.main.id
  direction     = "INGRESS"
  source_ranges = ["35.235.240.0/20"]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# Tráfego interno banco<->serviço<->gerador — restrito à própria sub-rede;
# tudo mais fica negado pela regra implícita de deny-all do GCP.
resource "google_compute_firewall" "allow_internal" {
  name          = "tcc-recsys-allow-internal${var.name_suffix}"
  network       = google_compute_network.main.id
  direction     = "INGRESS"
  source_ranges = [var.subnet_cidr]

  allow {
    protocol = "tcp"
  }
  allow {
    protocol = "udp"
  }
}

# Saída à internet para VMs sem IP público (pull de imagem Docker, gcloud,
# apt) — sem isto, instâncias sem access_config não teriam rota de saída.
resource "google_compute_router" "main" {
  name    = "tcc-recsys-router${var.name_suffix}"
  network = google_compute_network.main.id
  region  = var.region
}

resource "google_compute_router_nat" "main" {
  name                               = "tcc-recsys-nat${var.name_suffix}"
  router                             = google_compute_router.main.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"
}

output "network_id" {
  description = "ID da VPC."
  value       = google_compute_network.main.id
}

output "subnetwork_self_link" {
  description = "Self-link da sub-rede privada — exigido por network_interface.subnetwork nos módulos database/service/loadgen."
  value       = google_compute_subnetwork.private.self_link
}
