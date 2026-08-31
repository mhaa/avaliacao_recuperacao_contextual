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

variable "dataset_bucket" {
  type        = string
  description = "Bucket com a massa de dados completa (output do bootstrap: dataset_bucket) — schemas/<db>/load_full_dataset.py baixa daqui via harness/fixtures.py:ensure_full_dataset_downloaded."
}

variable "machine_type" {
  type        = string
  description = "Tipo de máquina — n2-standard-8 (IMPLEMENTACAO.md, topologia)."
  default     = "n2-standard-8"
}

resource "google_service_account" "loadgen" {
  account_id   = "tcc-${var.cell}-loadgen"
  display_name = "tcc-recsys loadgen service account (${var.cell})"
}

# tools_image é privada (Artifact Registry, não Docker Hub) — sem isso, o
# `docker pull` no boot falha com "Unauthenticated request" (mesmo bug já
# confirmado e corrigido em infra/modules/service/main.tf).
resource "google_project_iam_member" "loadgen_artifact_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.loadgen.email}"
}

# Leitura só — harness/fixtures.py:ensure_full_dataset_downloaded baixa,
# nunca escreve. Resultados saem por um caminho totalmente separado
# (infra/scripts/run_measurement_battery.py:upload_results_to_bucket, do
# HOST com a sessão do operador, não desta SA).
resource "google_storage_bucket_iam_member" "loadgen_dataset_reader" {
  bucket = var.dataset_bucket
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.loadgen.email}"
}

# Instrumentação de gargalo (CONTEXTO.md / analysis/resources.py:
# GCPMonitoringCollector) — mesma métrica de CPU que
# load/saturation.py:GENERATOR_CPU_THRESHOLD checa a cada patamar da busca
# de saturação, não só ao final; escrita sob esta SA.
resource "google_project_iam_member" "loadgen_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.loadgen.email}"
}

locals {
  # Mesma extração de infra/modules/service/main.tf — host do registro a
  # partir da própria referência da imagem, sem variável nova.
  tools_registry_host = split("/", var.tools_image)[0]

  # Instrumentação de gargalo (CONTEXTO.md) — mesmo mecanismo de
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
    # Mesmo motivo do retry de docker pull mais abaixo (imagem tools): a
    # primeira conexão de saída de uma VM nova pode dar timeout antes do
    # Cloud NAT estabilizar — confirmado ao vivo (console serial: "request
    # canceled while waiting for connection"). Sem retry aqui, essa falha sob
    # set -euo pipefail matava o startup-script INTEIRO antes até de chegar
    # no docker login/pull do loadgen — raiz real de timeouts de
    # wait_for_container que pareciam ser do próprio container do banco/
    # serviço.
    for i in 1 2 3 4 5; do docker pull mirror.gcr.io/otel/opentelemetry-collector-contrib:0.112.0 && break || sleep 10; done
    docker run -d --name tcc-otel-agent --restart unless-stopped \
      --pid host --network host \
      -v /:/hostfs:ro \
      -v /etc/otel-config.yaml:/etc/otelcol-contrib/config.yaml:ro \
      mirror.gcr.io/otel/opentelemetry-collector-contrib:0.112.0
  OTELEOT
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

  service_account {
    email  = google_service_account.loadgen.email
    scopes = ["cloud-platform"]
  }

  metadata = {
    startup-script = <<-EOT
      #!/bin/bash
      set -euo pipefail
      ${local.otel_collector_startup}
      # COS não tem gcloud — autentica o Docker direto via token OAuth do
      # servidor de metadados (mesmo padrão de infra/modules/service e
      # infra/modules/database).
      ACCESS_TOKEN=$(curl -sf -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" | sed -n 's/.*"access_token": *"\([^"]*\)".*/\1/p')
      # DOCKER_CONFIG em /tmp: /root também é raiz somente-leitura no COS
      # — "docker login" sem isso falha tentando gravar
      # /root/.docker/config.json (confirmado rodando de verdade).
      export DOCKER_CONFIG=/tmp/.docker
      echo "$ACCESS_TOKEN" | docker login -u oauth2accesstoken --password-stdin "https://${local.tools_registry_host}"
      # A primeira conexão de saída de uma VM nova pode dar timeout antes
      # do Cloud NAT/rede estabilizar (~20-30s de boot) — confirmado
      # reproduzindo 2x seguidas contra o Docker Hub. Retry evita destruir
      # e tentar de novo manualmente por causa disso.
      # 10x15s (150s), não 5x10s (50s): confirmado ao vivo, 3 falhas
      # consecutivas com "artifactregistry.repositories.downloadArtifacts"
      # negado numa região nunca usada antes pelo projeto (us-east4) — a
      # concessão de roles/artifactregistry.reader pra uma service account
      # recém-criada (recriada do zero a cada tentativa, já que o nome é
      # fixo por storage) às vezes leva mais que 50s pra propagar.
      for i in 1 2 3 4 5 6 7 8 9 10; do docker pull ${var.tools_image} && break || sleep 15; done
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

# Numérico, não o nome — mesmo motivo de infra/modules/database/main.tf:
# resource.labels.instance_id do Cloud Monitoring é o ID numérico da VM.
output "instance_id" {
  description = "ID numérico da VM do gerador de carga — para filtrar métricas no Cloud Monitoring."
  value       = google_compute_instance.loadgen.instance_id
}
