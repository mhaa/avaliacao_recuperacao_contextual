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
  description = "Referência completa da imagem em Artifact Registry (ex.: us-east4-docker.pkg.dev/PROJECT/tcc/service:TAG)."
}

variable "database_internal_ip" {
  type        = string
  description = "IP interno da VM de banco desta célula (output do módulo database)."
}

variable "machine_type" {
  type        = string
  description = "Tipo de máquina — n2-standard-4 (docs/ARCHITECTURE.md, topologia)."
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

# `service_image` é privada (Artifact Registry, não Docker Hub) — sem
# isso, o `docker pull` no boot falha com "Unauthenticated request"
# (confirmado rodando de verdade contra uma VM real).
resource "google_project_iam_member" "service_artifact_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.service.email}"
}

# Instrumentação de gargalo (docs/DESIGN.md / analysis/resources.py:
# GCPMonitoringCollector) — o coletor OpenTelemetry no startup-script
# escreve métrica de memória sob esta SA.
resource "google_project_iam_member" "service_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.service.email}"
}

locals {
  # Host do registro extraído da própria referência da imagem (ex.:
  # "us-east4-docker.pkg.dev" de
  # "us-east4-docker.pkg.dev/PROJECT/tcc/service:latest") — evita uma
  # variável nova só para repetir o que `service_image` já contém.
  service_registry_host = split("/", var.service_image)[0]

  # Instrumentação de gargalo (docs/DESIGN.md) — mesmo mecanismo de
  # infra/modules/database/main.tf (Ops Agent oficial não roda em COS;
  # OpenTelemetry Collector Contrib como contêiner). Duplicado idêntico
  # nos 3 módulos de propósito — não é configuração por célula.
  otel_collector_startup = <<-OTELEOT
    cat > /etc/otel-config.yaml <<'YAMLEOF'
    receivers:
      hostmetrics:
        collection_interval: 60s
        root_path: /hostfs
        scrapers:
          memory:
          cpu:
          network:
    processors:
      batch:
      # resourcedetection é obrigatório — ver infra/modules/database/main.tf
      # para o motivo completo (sem ele, as 3 VMs colidem no mesmo recurso
      # "generic_node" em branco no Cloud Monitoring, causando "written too
      # frequently"/"points out of order" — confirmado ao vivo).
      resourcedetection:
        detectors: [gcp]
        timeout: 10s
    exporters:
      googlecloud:
        project: "${var.project_id}"
    service:
      pipelines:
        metrics:
          receivers: [hostmetrics]
          processors: [resourcedetection, batch]
          exporters: [googlecloud]
    YAMLEOF
    # Mesmo motivo do retry de docker pull mais abaixo (imagem service/tools):
    # a primeira conexão de saída de uma VM nova pode dar timeout antes do
    # Cloud NAT estabilizar — confirmado ao vivo (console serial: "request
    # canceled while waiting for connection"). Sem retry aqui, essa falha sob
    # set -euo pipefail matava o startup-script INTEIRO antes até de chegar
    # no docker run do serviço — raiz real de timeouts de wait_for_container
    # que pareciam ser do próprio container do banco/serviço.
    for i in 1 2 3 4 5; do docker pull mirror.gcr.io/otel/opentelemetry-collector-contrib:0.112.0 && break || sleep 10; done
    docker run -d --name tcc-otel-agent --restart unless-stopped \
      --pid host --network host \
      -v /:/hostfs:ro \
      -v /etc/otel-config.yaml:/etc/otelcol-contrib/config.yaml:ro \
      mirror.gcr.io/otel/opentelemetry-collector-contrib:0.112.0
  OTELEOT
}

resource "google_compute_instance" "service" {
  # depends_on explícito na concessão de IAM: mesmo raciocínio de
  # infra/modules/loadgen/main.tf — o bloco service_account abaixo só
  # referencia google_service_account.service.email, então o Terraform
  # garante apenas que a SERVICE ACCOUNT existe antes da VM, não que a
  # concessão roles/artifactregistry.reader já foi criada. Sem isso, a VM
  # pode nascer (e já tentar `docker pull` de service_image, que é
  # privada) em paralelo com a concessão de IAM ainda propagando.
  depends_on = [google_project_iam_member.service_artifact_reader]

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
    # Sem access_config: sem IP público (docs/ARCHITECTURE.md).
  }

  service_account {
    email  = google_service_account.service.email
    scopes = ["cloud-platform"]
  }

  metadata = {
    startup-script = <<-EOT
      #!/bin/bash
      set -euo pipefail
      ${local.otel_collector_startup}
      # COS não tem o Cloud SDK (`gcloud`) instalado — confirmado na
      # prática. Busca a senha direto na API do Secret Manager,
      # autenticado com o token da conta de serviço da VM via servidor de
      # metadados (o único jeito de fazer isso sem gcloud). Mesmo padrão
      # de infra/modules/database/main.tf.
      # Padrão do sed tolera espaço opcional depois dos dois-pontos: o
      # endpoint de token do metadados devolve JSON compacto, mas a API
      # do Secret Manager devolve formatada ("data": "...", com espaço)
      # — confirmado inspecionando a resposta real numa VM.
      ACCESS_TOKEN=$(curl -sf -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" | sed -n 's/.*"access_token": *"\([^"]*\)".*/\1/p')
      POSTGRES_PASSWORD=$(curl -sf -H "Authorization: Bearer $ACCESS_TOKEN" "https://secretmanager.googleapis.com/v1/projects/${var.project_id}/secrets/tcc-postgres-password/versions/latest:access" | sed -n 's/.*"data": *"\([^"]*\)".*/\1/p' | base64 -d)
      # service_image é privada (Artifact Registry) — autentica o Docker
      # com o mesmo token OAuth já buscado acima (mesmo escopo
      # cloud-platform cobre Secret Manager e Artifact Registry).
      # DOCKER_CONFIG em /tmp: /root também é raiz somente-leitura no COS
      # — "docker login" sem isso falha tentando gravar
      # /root/.docker/config.json (confirmado rodando de verdade).
      export DOCKER_CONFIG=/tmp/.docker
      echo "$ACCESS_TOKEN" | docker login -u oauth2accesstoken --password-stdin "https://${local.service_registry_host}"
      # A primeira conexão de saída de uma VM nova pode dar timeout antes
      # do Cloud NAT/rede estabilizar (~20-30s de boot) — confirmado
      # reproduzindo 2x seguidas contra o Docker Hub (mesmo mecanismo,
      # ainda que aqui seja Artifact Registry). Retry evita destruir e
      # tentar de novo manualmente por causa disso.
      # 10x15s, mesmo motivo do módulo loadgen — pull autenticado no
      # Artifact Registry sofre do mesmo risco de propagação de IAM lenta.
      for i in 1 2 3 4 5 6 7 8 9 10; do docker pull ${var.service_image} && break || sleep 15; done
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

# Numérico, não o nome — mesmo motivo de infra/modules/database/main.tf:
# resource.labels.instance_id do Cloud Monitoring é o ID numérico da VM.
output "instance_id" {
  description = "ID numérico da VM de serviço — para filtrar métricas no Cloud Monitoring."
  value       = google_compute_instance.service.instance_id
}
