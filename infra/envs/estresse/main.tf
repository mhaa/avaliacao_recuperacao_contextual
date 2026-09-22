# Campanha COMPLEMENTAR de estresse com foco no banco (docs/DESIGN.md,
# "Experimento complementar — estresse com foco no banco").
#
# Root module PRÓPRIO, e não uma flag em ../experiment, porque o isolamento
# precisa ser estrutural e não procedimental. Quatro camadas:
#
#   1. Este arquivo. `infra/envs/experiment/main.tf` não é tocado em nenhuma
#      linha — `git diff` naquele diretório prova.
#   2. Estado remoto separado: prefixo `estresse/<cell>`, contra `cells/<cell>`
#      da campanha principal. Os dois estados nunca se abrem mutuamente. Sem
#      isso, um `apply` daqui ESCREVERIA no estado da célula principal, e um
#      destroy interrompido a deixaria registrada com máquinas de 32 vCPU.
#   3. TF_DATA_DIR separado (`.terraform-estresse-<cell>`), pelo motivo
#      documentado em infra/scripts/cloud_smoke_test.py:142-158 — um
#      `.terraform/` compartilhado já fez um destroy rodar contra a célula
#      errada, reportar "0 destroyed" e deixar 3 VMs faturando.
#   4. `name_suffix = "-st"` em todos os módulos: mesmo que as duas
#      infraestruturas coexistissem, não há colisão de nome de VM, service
#      account, disco, VPC ou regra de firewall.
#
# O que este env NÃO faz, de propósito: a VM de BANCO mantém o
# `n2-standard-8` padrão e a configuração interna idêntica à da campanha
# principal. É ela que se quer saturar — mexer nela trocaria a pergunta.
#
# Uso:
#   terraform -chdir=infra/envs/estresse init -reconfigure \
#     -backend-config="bucket=<state bucket>" \
#     -backend-config="prefix=estresse/${cell}"
#   terraform -chdir=infra/envs/estresse plan    # NUNCA apply sem confirmação — faturável

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
  backend "gcs" {
    # bucket e prefix vêm de -backend-config, como no env principal.
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
  description = "Região única do experimento."
  default     = "us-east4"
}

variable "zone" {
  type        = string
  description = "Zona dentro da região."
  default     = "us-east4-a"
}

variable "cell" {
  type        = string
  description = <<-EOT
    Célula sob estresse. Restrita às 4 já confirmadas na campanha principal:
    a projeção do joelho de cada uma vem da CPU de banco medida lá
    (results/report/extra/resource_bottleneck_by_cell_tier.csv), então uma
    célula sem esse dado não teria como dimensionar a rampa.
  EOT
  validation {
    condition     = contains(["e2-scylla", "e3-postgres", "e3-valkey", "e4-valkey"], var.cell)
    error_message = "cell precisa ser uma das 4 células confirmadas: e2-scylla, e3-postgres, e3-valkey, e4-valkey."
  }
}

variable "storage" {
  type        = string
  description = "Tecnologia de banco da célula."
  validation {
    condition     = contains(["postgres", "valkey", "scylla", "opensearch"], var.storage)
    error_message = "storage precisa ser postgres, valkey, scylla ou opensearch."
  }
}

variable "service_image" {
  type        = string
  description = "Imagem do serviço no Artifact Registry."
}

variable "tools_image" {
  type        = string
  description = "Imagem de ferramentas (k6 + análise) no Artifact Registry."
}

variable "dataset_bucket" {
  type        = string
  description = "Bucket com a massa de dados."
}

variable "results_bucket" {
  type        = string
  description = "Bucket de resultados — a campanha grava sob o prefixo _estresse/."
}

variable "data_disk_snapshot" {
  type        = string
  description = "Snapshot do dataset; vazio força carga completa (sempre o caso do Valkey)."
  default     = ""
}

variable "service_machine_type" {
  type        = string
  description = <<-EOT
    O ponto inteiro da campanha: escalar o serviço em vCPUs até que o BANCO
    vire o gargalo. Dimensionado por célula, a partir da CPU de banco medida
    na confirmação e do teto arquitetural de cada tecnologia (Scylla --smp 7
    -> ~87,5% da VM; Postgres -> 100%; Valkey, thread única, -> ~12,5%):

      e4-valkey    n2-custom-16-32768   (16 vCPU / 32 GB, 2x)
      e2-scylla    n2-highcpu-32        (32 vCPU / 32 GB, 4x)
      e3-postgres  n2-highcpu-64        (64 vCPU / 64 GB, 8x)
      e3-valkey    n2-highcpu-96        (96 vCPU / 96 GB, 12x)

    Nenhuma reduz os 32 GB atuais. service/http_app.py usa
    `config.workers = os.cpu_count()`, então a escala é automática e nenhuma
    imagem precisa ser reconstruída — mas cada worker carrega sua cópia do
    catálogo (~20-40 MB), o que a 96 workers dá ~3,8 GB.
  EOT
  default     = "n2-standard-8"
}

variable "loadgen_machine_type" {
  type        = string
  description = <<-EOT
    O gerador escala junto, ou vira ele o gargalo: load/saturation.py aborta a
    60% de CPU do gerador, e um n2-standard-8 não sustenta 11-38k req/s. Se
    ele saturar antes, a curva medida é do gerador e não do banco — execução
    inválida, não dado.
  EOT
  default     = "n2-standard-8"
}

variable "loadgen_boot_disk_gb" {
  type        = number
  description = <<-EOT
    Só precisa crescer se o pré-voo de load/ramp.py:check_disk_budget pedir.
    Em condição normal 100 GB bastam: a rampa não grava k6-raw.json, que era
    o que dominava o volume (~524 B/requisição contra 134 B da NDJSON).
  EOT
  default     = 100
}

# Sufixo curto por imposição do GCP: o ID de service account tem teto de 30
# caracteres e "tcc-e1-opensearch-loadgen" já usa 25. "-st" fecha em 28;
# "-estresse" daria 34 e o apply falharia.
locals {
  name_suffix = "-st"
}

provider "google" {
  project = var.project_id
  region  = var.region
}

module "network" {
  source      = "../../modules/network"
  project_id  = var.project_id
  region      = var.region
  name_suffix = local.name_suffix
}

module "database" {
  source               = "../../modules/database"
  project_id           = var.project_id
  zone                 = var.zone
  cell                 = var.cell
  storage              = var.storage
  subnetwork_self_link = module.network.subnetwork_self_link
  data_disk_snapshot   = var.data_disk_snapshot
  name_suffix          = local.name_suffix
  # machine_type omitido de propósito: a VM de banco é o objeto de estudo.
}

module "service" {
  source               = "../../modules/service"
  project_id           = var.project_id
  zone                 = var.zone
  cell                 = var.cell
  subnetwork_self_link = module.network.subnetwork_self_link
  service_image        = var.service_image
  database_internal_ip = module.database.internal_ip
  machine_type         = var.service_machine_type
  name_suffix          = local.name_suffix
}

module "loadgen" {
  source               = "../../modules/loadgen"
  project_id           = var.project_id
  zone                 = var.zone
  cell                 = var.cell
  subnetwork_self_link = module.network.subnetwork_self_link
  tools_image          = var.tools_image
  dataset_bucket       = var.dataset_bucket
  results_bucket       = var.results_bucket
  machine_type         = var.loadgen_machine_type
  boot_disk_size_gb    = var.loadgen_boot_disk_gb
  name_suffix          = local.name_suffix
}

output "database_internal_ip" {
  description = "IP interno da VM de banco."
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

output "database_instance_id" {
  description = "ID numérico da VM de banco — para filtrar métricas no Cloud Monitoring."
  value       = module.database.instance_id
}

output "service_instance_id" {
  description = "ID numérico da VM de serviço — para filtrar métricas no Cloud Monitoring."
  value       = module.service.instance_id
}

output "loadgen_instance_id" {
  description = "ID numérico da VM do gerador — para filtrar métricas no Cloud Monitoring."
  value       = module.loadgen.instance_id
}
