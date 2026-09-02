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

variable "data_disk_snapshot" {
  type        = string
  description = "Nome do snapshot pra criar o disco já carregado (infra/scripts/seed_dataset_snapshots.py, ex.: tcc-dataset-seed-postgres) — vazio cria disco em branco (comportamento padrão)."
  default     = ""
}

# Mesma imagem/flags de docker-compose.yml — nada de configuração nova
# inventada aqui, só traduzida para `docker run` (evita confundir a
# comparação entre células com uma diferença acidental de configuração).
locals {
  # mirror.gcr.io/... em vez do Docker Hub direto: mirror público e gratuito
  # do Google, sem autenticação extra (continua anônimo, só troca de onde
  # vem) — confirmado ao vivo que o Docker Hub direto começou a recusar TODAS
  # as 5 tentativas do retry loop com "context deadline exceeded" numa VM
  # nova, provável rate limit de pulls anônimos por IP (o projeto criou
  # dezenas de VMs hoje, todas saindo pelo mesmo Cloud NAT).
  docker_image = {
    postgres   = "mirror.gcr.io/library/postgres:16-alpine"
    valkey     = "mirror.gcr.io/valkey/valkey:8-alpine"
    scylla     = "mirror.gcr.io/scylladb/scylla:6.2"
    opensearch = "mirror.gcr.io/opensearchproject/opensearch:2.18.0"
  }
  # /mnt/disks/data, não /mnt/data: no COS a raiz é somente-leitura
  # (dm-verity) — só /mnt/disks (e /var, /home) são graváveis. Confirmado
  # na prática (mkdir em /mnt/data falhava com "Read-only file system").
  #
  # docker_run_flags = só opções de verdade do `docker run` (-p/-v/-e),
  # colocadas ANTES do nome da imagem. docker_command_args = argumentos do
  # processo do próprio container (postgres/valkey-server/scylla), que
  # precisam vir DEPOIS da imagem — ver docker-compose.yml local, onde
  # cada um já é um `command:` YAML separado. Misturar os dois antes da
  # imagem quebra na prática: `docker run` tentou interpretar
  # `-c shared_buffers=1GB` como a própria flag `-c`/`--cpu-shares` dele
  # (que espera um inteiro), não como argumento do Postgres — confirmado
  # rodando de verdade contra uma VM real.
  docker_run_flags = {
    # PGDATA numa subpasta, não a raiz do volume: montar um disco recém-
    # formatado direto como data dir faz o initdb recusar ("directory
    # ... exists but is not empty" — o ext4 sempre tem um lost+found na
    # raiz). Nunca aparece localmente porque docker-compose.yml usa um
    # volume nomeado do Docker, não um disco bruto — confirmado rodando
    # de verdade contra uma VM real.
    # --shm-size=2g: o /dev/shm padrão de um container Docker é 64MB, e o
    # Postgres usa dynamic shared memory para coordenar parallel workers.
    # Confirmado ao vivo na semeadura em us-east4: o VACUUM ANALYZE ao fim
    # da carga completa (138.640.226 linhas em prematerialized) paralelizou
    # e morreu com `could not resize shared memory segment ... to 67128896
    # bytes: No space left on device` — 67128896 é 64,02MB, ou seja, pediu
    # um triz mais que TODO o /dev/shm. Não é o disco de dados (200GB, que
    # tinha espaço de sobra).
    #
    # docker-compose.yml já declarava `shm_size: 1gb` para o postgres local
    # desde sempre; era a NUVEM que tinha ficado para trás, herdando o
    # default de 64MB do Docker. Mais um caso do mesmo padrão de divergência
    # local↔nuvem já documentado abaixo em --smp/--memory do Scylla e no
    # heap do OpenSearch. 2g (e não 1g como no local) pelo mesmo motivo de
    # todos os outros valores daqui: a VM é exclusiva do banco e tem 32GB.
    #
    # Vale além do VACUUM: com o teto de 64MB, qualquer plano paralelo
    # durante a própria medição correria o mesmo risco.
    postgres = "-p 5432:5432 --shm-size=2g -v /mnt/disks/data:/var/lib/postgresql/data -e POSTGRES_USER=tcc -e POSTGRES_DB=recsys -e PGDATA=/var/lib/postgresql/data/pgdata"
    valkey   = "-p 6379:6379"
    # --cap-add SYS_NICE: exigido por --overprovisioned 0 (ver
    # docker_command_args abaixo) — sem ele o Seastar não consegue fazer
    # mbind/afinidade das shards e avisa "unable to mbind shard memory;
    # performance may suffer", justamente o que --overprovisioned 0 quer
    # habilitar. Confirmado localmente: com a capability o aviso some.
    scylla = "-p 9042:9042 --cap-add SYS_NICE -v /mnt/disks/data:/var/lib/scylla"
    # OPENSEARCH_JAVA_OPTS precisa de aspas em volta do valor inteiro: sem
    # elas, o shell quebra "-Xms2g -Xmx2g" em dois tokens e o segundo
    # ("-Xmx2g") chega ao `docker run` como se fosse uma flag própria dele
    # ("unknown shorthand flag: 'X'") — confirmado rodando de verdade.
    #
    # Heap 16g (50% dos 32GB da VM, teto recomendado pelo próprio
    # Elasticsearch/OpenSearch — acima de ~32GB perde-se compressed oops),
    # não 2g (valor de dev local, mesmo padrão de sub-provisionamento já
    # encontrado no Scylla — nunca chegou a rodar de verdade em nuvem pra
    # ter sido pego antes). --ulimit memlock=-1:-1 adicionado junto: com
    # bootstrap.memory_lock=true e um heap bem maior, é bem mais provável
    # esbarrar no ulimit padrão do container e falhar o mlockall na
    # inicialização — exigido pela própria documentação do OpenSearch
    # quando memory_lock está ligado.
    opensearch = "-p 9200:9200 -v /mnt/disks/data:/usr/share/opensearch/data -e discovery.type=single-node -e bootstrap.memory_lock=true -e OPENSEARCH_JAVA_OPTS=\"-Xms16g -Xmx16g\" -e DISABLE_SECURITY_PLUGIN=true --ulimit memlock=-1:-1"
  }
  docker_command_args = {
    # shared_buffers=8GB (25% dos 32GB — orientação padrão do Postgres),
    # effective_cache_size=24GB (avisa o planner quanto de cache de SO
    # esperar, sem alocar nada) e work_mem=256MB, não os valores de dev
    # local (1GB/64MB) — mesmo padrão de sub-provisionamento do Scylla/
    # OpenSearch, encontrado numa auditoria proativa depois de achar os
    # outros dois. Menos severo que Scylla/OpenSearch (Postgres ainda se
    # beneficia do cache de página do próprio SO mesmo com shared_buffers
    # pequeno, diferente de motores que travam memória exclusiva), mas
    # ainda deixa memória real da VM sem uso. e1-postgres já tinha dado
    # real coletado com o valor antigo — precisa ser remedido com este.
    # maintenance_work_mem=2GB: parâmetro SEPARADO de work_mem (que vale só
    # para consultas) — é o que VACUUM/ANALYZE/CREATE INDEX usam. No default
    # de 64MB, o VACUUM ANALYZE do fim da carga completa varre os índices de
    # centenas de milhões de linhas em várias passadas, transformando o
    # último passo da semeadura em horas. A VM tem 32GB e é exclusiva do
    # banco; 2GB aqui é folgado mesmo com os 3 autovacuum workers padrão
    # (autovacuum_work_mem herda este valor) somados aos 8GB de
    # shared_buffers.
    postgres = "-c shared_buffers=8GB -c effective_cache_size=24GB -c work_mem=256MB -c maintenance_work_mem=2GB -c max_connections=200 -c random_page_cost=1.1 -c track_io_timing=on"
    valkey   = "--save \"\" --appendonly no --maxmemory 24gb --maxmemory-policy noeviction"
    # --smp 7 --memory 28G, não --smp 1 --memory 2G (valor herdado do
    # docker-compose.yml LOCAL, CLAUDE.md: "só correção, nunca medição de
    # desempenho"): confirmado ao vivo — na VM de nuvem (n2-standard-8, 8
    # vCPUs/32GB), o Scylla ficava preso a 1 core e 2GB, usando só ~13% de
    # CPU real, carregando o dataset completo em mais de 1 hora quando
    # deveria ser questão de minutos. Além de lento, isso corrompe a
    # REPRESENTATIVIDADE da medição de latência real — a VM inteira é
    # exclusiva do Scylla, não faz sentido sufocá-lo do mesmo jeito que no
    # dev local compartilhado. --overprovisioned/--developer-mode mantidos
    # (não são o gargalo de performance, e mexer neles arrisca o Scylla
    # recusar iniciar no COS sem o tuning completo de I/O de produção).
    # --overprovisioned 0 (explícito, não omitido): afirma que a VM é
    # EXCLUSIVA do banco, ligando afinidade de CPU e polling agressivo —
    # a mesma premissa que já justifica --smp 7 --memory 28G. Antes estava
    # --overprovisioned 1, que afirma o contrário e contradizia o resto da
    # linha. Exige --cap-add SYS_NICE em docker_run_flags: sem isso o
    # Seastar falha o mbind das shards ("unable to mbind shard memory;
    # performance may suffer") — confirmado localmente.
    #
    # ATENÇÃO ao valor "0": omitir a flag NÃO desliga nada. O parser da
    # própria imagem (/commandlineparser.py) tem default '1' para
    # --developer-mode e, para --overprovisioned, "roda em modo
    # overprovisioned por padrão a menos que --cpuset seja especificado" —
    # ou seja, simplesmente apagar as flags mantinha os dois LIGADOS,
    # silenciosamente. Confirmado lendo a linha de comando real no log.
    #
    # --developer-mode continua 1 por ora, apesar de a documentação do
    # Scylla desaconselhar avaliar desempenho com ele: desligar exige o
    # diretório de dados em XFS (o iotune recusa outro filesystem: "did
    # not pass validation tests, it may not be on XFS"), e o disco aqui é
    # formatado ext4 mais abaixo. Trocar para XFS implicaria também
    # re-gerar o snapshot semeado do Scylla (que carrega o filesystem
    # dentro) e uma ferramenta de mkfs.xfs, ausente tanto no COS quanto na
    # imagem do Scylla. Testado localmente: com --developer-mode 0 o
    # container morre no boot exatamente com esse erro.
    scylla = "--smp 7 --memory 28G --overprovisioned 0 --skip-wait-for-gossip-to-settle 0"
    # indices.memory.index_buffer_size é setting estático (só via config no
    # boot, não muda em runtime pela API) — default é 10% do heap (1.6GB
    # com os 16GB configurados acima). 25% (4GB) reduz a frequência de
    # flush de segmento durante a carga de ~100M documentos (schemas/
    # opensearch/load_full_dataset.py); `-E` é o mecanismo suportado pela
    # própria imagem oficial do OpenSearch para passar overrides de
    # settings estáticos via CMD, sem editar opensearch.yml.
    opensearch = "-Eindices.memory.index_buffer_size=25%"
  }
}

# Instrumentação de gargalo (CONTEXTO.md) — Ops Agent oficial do Google não
# roda em COS (sem apt/yum); usamos o OpenTelemetry Collector Contrib como
# contêiner (hostmetrics + exporter googlecloud), lendo o sistema de
# arquivos do host via /hostfs somente leitura. Duplicado idêntico nos 3
# módulos (database/service/loadgen) — não é configuração por célula, não
# vale a pena uma abstração Terraform só para isso. Métrica exata ainda sem
# validação contra uma VM real — ver README.md, "Riscos conhecidos".
locals {
  otel_collector_startup = <<-OTELEOT
    cat > /etc/otel-config.yaml <<'YAMLEOF'
    receivers:
      hostmetrics:
        # 60s: mínimo que o Cloud Monitoring aceita entre pontos de uma
        # métrica customizada (workload.googleapis.com/*) — mantido mesmo
        # depois de achar a causa raiz abaixo, é boa prática documentada
        # pelo próprio Google independente dela.
        collection_interval: 60s
        root_path: /hostfs
        scrapers:
          memory:
          cpu:
          network:
    processors:
      batch:
      # SEM ISSO, o exporter googlecloud não sabe que está rodando numa VM
      # específica do Compute Engine — cai no fallback resource.type=
      # "generic_node" com labels EM BRANCO (node_id="", namespace="",
      # location="global"). Como as 3 VMs (banco/serviço/loadgen) caem
      # todas nesse mesmo recurso "em branco", o Cloud Monitoring recebe
      # escritas de fontes diferentes como se fossem a MESMA série temporal
      # — confirmado ao vivo via `docker logs tcc-otel-agent` + SSH: a 5s
      # de intervalo dava "written more frequently than the maximum
      # sampling period" (colisão rápida entre VMs), a 60s dava "Points
      # must be written in order" (uma VM escrevendo um timestamp mais
      # velho que o que outra VM acabou de escrever pro mesmo recurso
      # colapsado) — dois sintomas da MESMA causa, não dois bugs
      # diferentes. `resourcedetection` com o detector `gcp` faz cada
      # coletor se identificar como o `gce_instance` certo (via metadata
      # server, alcançável por --network host), eliminando a colisão.
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
    # Mesmo motivo do retry de docker_image[storage] mais abaixo: a primeira
    # conexão de saída de uma VM nova pode dar timeout antes do Cloud NAT
    # estabilizar — confirmado ao vivo (console serial: "request canceled
    # while waiting for connection"). Sem retry aqui, essa falha sob
    # set -euo pipefail matava o startup-script INTEIRO antes até de chegar
    # no docker run do banco — raiz real de timeouts de wait_for_container
    # que pareciam ser do próprio container do banco/serviço.
    for i in 1 2 3 4 5; do docker pull mirror.gcr.io/otel/opentelemetry-collector-contrib:0.112.0 && break || sleep 10; done
    docker run -d --name tcc-otel-agent --restart unless-stopped \
      --pid host --network host \
      -v /:/hostfs:ro \
      -v /etc/otel-config.yaml:/etc/otelcol-contrib/config.yaml:ro \
      mirror.gcr.io/otel/opentelemetry-collector-contrib:0.112.0
  OTELEOT
}

resource "google_compute_disk" "data" {
  name     = "tcc-${var.cell}-data"
  zone     = var.zone
  size     = var.data_disk_size_gb
  type     = "pd-ssd"
  snapshot = var.data_disk_snapshot != "" ? var.data_disk_snapshot : null
}

resource "google_service_account" "database" {
  account_id   = "tcc-${var.cell}-database"
  display_name = "tcc-recsys database service account (${var.cell})"
}

# Só a célula postgres usa isso (busca a própria senha no boot, ver
# startup-script abaixo) — concedido incondicionalmente aos 4 bancos
# porque é mais simples que condicionar por storage e não custa nada nos
# outros 3, que nunca chamam a API. Mesmo padrão de
# infra/modules/service/main.tf: só o necessário, nada mais.
resource "google_project_iam_member" "database_secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.database.email}"
}

# Instrumentação de gargalo (CONTEXTO.md / analysis/resources.py:
# GCPMonitoringCollector) — o coletor OpenTelemetry no startup-script
# escreve métrica de memória sob esta SA.
resource "google_project_iam_member" "database_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = "serviceAccount:${google_service_account.database.email}"
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

  service_account {
    email  = google_service_account.database.email
    scopes = ["cloud-platform"]
  }

  metadata = {
    startup-script = <<-EOT
      #!/bin/bash
      set -euo pipefail
      mkdir -p /mnt/disks/data
      %{if var.data_disk_snapshot == ""}
      # Só formata disco em branco — um disco restaurado de snapshot
      # (infra/scripts/seed_dataset_snapshots.py) já tem filesystem e
      # dados; formatar de novo apagaria a carga que acabou de ser
      # restaurada.
      mkfs.ext4 -F /dev/disk/by-id/google-data 2>/dev/null || true
      %{endif}
      mount /dev/disk/by-id/google-data /mnt/disks/data
      ${local.otel_collector_startup}
      %{if var.storage == "opensearch"}
      # A imagem oficial do OpenSearch roda como usuário não-root (uid
      # 1000) e não ajusta a posse do diretório de dados montado — sem
      # isso o processo falha no boot com "AccessDeniedException:
      # /usr/share/opensearch/data/nodes" (confirmado rodando de
      # verdade). Postgres e Scylla fazem esse chown internamente no
      # próprio entrypoint (rodando como root antes de trocar de
      # usuário), então não precisam disso.
      chown -R 1000:1000 /mnt/disks/data
      %{endif}
      %{if var.storage == "postgres"}
      # COS não tem o Cloud SDK (`gcloud`) instalado — confirmado na
      # prática (nem no PATH). Busca a senha direto na API do Secret
      # Manager, autenticado com o token da conta de serviço da VM via
      # servidor de metadados (o único jeito de fazer isso sem gcloud).
      # Padrão do sed tolera espaço opcional depois dos dois-pontos: o
      # endpoint de token do metadados devolve JSON compacto, mas a API
      # do Secret Manager devolve formatada ("data": "...", com espaço)
      # — confirmado inspecionando a resposta real numa VM.
      ACCESS_TOKEN=$(curl -sf -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" | sed -n 's/.*"access_token": *"\([^"]*\)".*/\1/p')
      POSTGRES_PASSWORD=$(curl -sf -H "Authorization: Bearer $ACCESS_TOKEN" "https://secretmanager.googleapis.com/v1/projects/${var.project_id}/secrets/tcc-postgres-password/versions/latest:access" | sed -n 's/.*"data": *"\([^"]*\)".*/\1/p' | base64 -d)
      # A primeira conexão de saída de uma VM nova pode dar timeout antes
      # do Cloud NAT/rede estabilizar (~20-30s de boot) — confirmado
      # reproduzindo 2x seguidas contra o Docker Hub. Retry evita ter que
      # destruir e tentar de novo manualmente por causa disso.
      for i in 1 2 3 4 5; do docker pull ${local.docker_image["postgres"]} && break || sleep 10; done
      docker run -d --name tcc-database --restart unless-stopped \
        -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
        ${local.docker_run_flags["postgres"]} \
        ${local.docker_image["postgres"]} \
        ${local.docker_command_args["postgres"]}
      %{else}
      for i in 1 2 3 4 5; do docker pull ${local.docker_image[var.storage]} && break || sleep 10; done
      docker run -d --name tcc-database --restart unless-stopped \
        ${local.docker_run_flags[var.storage]} \
        ${local.docker_image[var.storage]} \
        ${local.docker_command_args[var.storage]}
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

# Numérico, não o nome (`tcc-<cell>-database`): resource.labels.instance_id
# do Cloud Monitoring para métricas gce_instance (ex.:
# compute.googleapis.com/instance/cpu/utilization) é o ID numérico da VM, não
# o nome — confirmado ao vivo (filtro por nome nunca casava nenhuma série,
# não era atraso de propagação como se pensou a princípio). Consumido por
# infra/scripts/run_measurement_battery.py.
output "instance_id" {
  description = "ID numérico da VM de banco — para filtrar métricas no Cloud Monitoring."
  value       = google_compute_instance.database.instance_id
}

output "data_disk_name" {
  description = "Nome do disco de dados — usado por infra/scripts/snapshot_after_load.sh."
  value       = google_compute_disk.data.name
}
