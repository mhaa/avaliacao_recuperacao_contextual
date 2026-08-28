# Rede privada única para banco, serviço e gerador de carga. Sem IP público
# em nenhuma VM (IMPLEMENTACAO.md, "Sem IP público nas VMs de banco e
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

resource "google_compute_network" "main" {
  name                    = "tcc-recsys-network"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "private" {
  name                     = "tcc-recsys-subnet"
  ip_cidr_range            = var.subnet_cidr
  region                   = var.region
  network                  = google_compute_network.main.id
  private_ip_google_access = true
}

# SSH só via faixa de IP do IAP (IMPLEMENTACAO.md) — nunca 0.0.0.0/0.
resource "google_compute_firewall" "allow_iap_ssh" {
  name          = "tcc-recsys-allow-iap-ssh"
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
  name          = "tcc-recsys-allow-internal"
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
  name    = "tcc-recsys-router"
  network = google_compute_network.main.id
  region  = var.region
}

resource "google_compute_router_nat" "main" {
  name                               = "tcc-recsys-nat"
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
