# Arquitetura de bancos de dados para recuperação em sistemas de recomendação

Este projeto compara arquiteturas de banco de dados para a etapa de
**recuperação** de dados em sistemas de recomendação: dado um ranking de
candidatos pré-computado por usuário, qual a forma mais eficiente de
devolver, em tempo de requisição, só o subconjunto elegível sob o contexto
da sessão (categoria, faixa de preço, disponibilidade, itens já vistos)?

São avaliadas 4 estratégias de recuperação (E-1..E-4) sobre 4 paradigmas de
armazenamento (BD-1..BD-4: PostgreSQL, Valkey, ScyllaDB, OpenSearch). Cada
combinação de estratégia e banco é uma "célula" — 14 no total (4×4, menos 2
combinações arquiteturalmente inviáveis). O repositório serve tanto como
registro do experimento quanto como uma base reprodutível para quem estiver
avaliando essas quatro tecnologias para esse tipo de carga de trabalho.

Para mais contexto:
- [docs/DESIGN.md](docs/DESIGN.md) — desenho experimental completo: parâmetros
  fixos, matriz de viabilidade, protocolo de medição, estatística, hipóteses.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — arquitetura do código:
  estrutura do repositório, abstrações centrais, práticas de Terraform.
- [docs/DECISIONS.md](docs/DECISIONS.md) — histórico de decisões e bugs reais
  encontrados durante a implementação, por etapa.
- [docs/BENCHMARKS.md](docs/BENCHMARKS.md) — expectativa de latência **antes**
  de medir: benchmarks públicos das 4 tecnologias e régua de diagnóstico para
  separar "resultado da tecnologia" de "erro de configuração".

## Passo a passo de reprodução

Desenvolvido em 5 fases, na ordem abaixo.

### Fase 1 — Preparação da massa de dados

Base MovieLens 32M com ranking offline via ALS (não é escopo deste projeto a
modelagem de aprendizado de máquina — ver [`data_generation/`](data_generation/)).

```
docker compose build generator
docker compose run --rm generator all --sample-users 10000 --seed 42
```

Escala de desenvolvimento (10.000 usuários); para a base real completa
(~200 mil usuários) omita `--sample-users`.

### Fase 2 — Serviço de recuperação (`core/`, `storage/`, `strategies/`)

Implementação das 14 células viáveis: 4 estratégias (E-1..E-4) sobre os 4
bancos (BD-1..BD-4). Ver [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) para as
abstrações (`StorageAdapter`, `Strategy`) e [docs/DECISIONS.md](docs/DECISIONS.md)
para a narrativa completa de cada etapa abaixo.

```
docker compose build tools
docker compose run --rm tools         # camada rápida, sem banco
```

> Argumentos passados a `docker compose run --rm tools <args>` **substituem**
> o `CMD` padrão da imagem, não se somam a ele — para rodar só a camada
> rápida com saída verbosa: `docker compose run --rm tools -m "not integration" -v`.

**Etapa 1 — `core/`**: contrato de requisição/resposta, ordenação com
desempate, exclusão de itens de sessão — regras compartilhadas por todas as
14 células.

**Etapa 2 — `storage/postgres.py` + `strategies/e1_app_filter.py`**: primeira
célula ponta a ponta (E-1 sobre Postgres). `storage/tests/fakes.py` traz um
adaptador fake em memória, usado pelos testes de estratégia sem banco algum.

```
docker compose up -d postgres
docker compose run --rm tools -m integration storage/tests/test_postgres_adapter.py -v
docker compose stop postgres
```

**Etapa 3 — `harness/`** (oráculo + verificação) — **obrigatória antes de
qualquer outra célula**. Compara a resposta de uma célula contra 1000 casos
com resultado esperado. Nenhuma célula é considerada pronta, e nenhuma
latência pode ser medida, sem passar 100% aqui (ver
[docs/DESIGN.md](docs/DESIGN.md#regra-de-ouro-da-implementação)).

```
docker compose up -d postgres
docker compose run --rm --entrypoint python tools schemas/postgres/load_oracle_fixture.py
docker compose run --rm tools -m integration -v
docker compose stop postgres
```

> **Cuidado ao escrever testes de integração contra Postgres:** nunca
> hardcode `item_id`s "de teste" dentro da faixa real do catálogo
> (`0..87584`) — pode corromper dado real carregado pela fixture. Ver
> [docs/DECISIONS.md#etapa-3](docs/DECISIONS.md#etapa-3) para o bug real que
> isso já causou.

**Etapa 4 — E-2, E-3, E-4 sobre Postgres**, cada uma verificada contra o
oráculo — fecha a primeira linha da matriz 4×4.

```
docker compose up -d postgres
docker exec -i tcc-postgres psql -U tcc -d recsys < schemas/postgres/002_prematerialized.sql
docker compose run --rm --entrypoint python tools schemas/postgres/load_oracle_fixture.py
docker compose run --rm tools -m integration tests/acceptance/test_harness_all_cells.py -v
docker compose stop postgres
```

**Etapa 5 — Valkey, ScyllaDB, OpenSearch**: as demais 3 tecnologias, cada uma
com suas 4 estratégias verificadas contra o oráculo. Fecha a verificação de
corretude da Fase 1: das 16 combinações, 14 são viáveis (todas passam pelos
1000 casos do oráculo) e 2 são inviáveis por design (E-4/Scylla, E-3/OpenSearch
— falham corretamente na montagem da célula, com mensagem clara). Ver
[docs/DECISIONS.md#etapa-5-valkey](docs/DECISIONS.md#etapa-5-valkey),
[#etapa-5-scylla](docs/DECISIONS.md#etapa-5-scylla) e
[#etapa-5-opensearch](docs/DECISIONS.md#etapa-5-opensearch) para a modelagem
de cada banco e os bugs reais encontrados (especialmente os 3 do Scylla, com
`IN` de partição e limites de batch).

```
docker compose up -d postgres valkey scylla opensearch
docker compose run --rm --entrypoint python tools schemas/scylla/apply_schema.py
docker compose run --rm --entrypoint python tools schemas/opensearch/create_index.py
docker compose run --rm --entrypoint python tools schemas/postgres/load_oracle_fixture.py
docker compose run --rm --entrypoint python tools schemas/valkey/load_oracle_fixture.py
docker compose run --rm --entrypoint python tools schemas/scylla/load_oracle_fixture.py
docker compose run --rm --entrypoint python tools schemas/opensearch/load_oracle_fixture.py
docker compose run --rm tools -m integration tests/acceptance/test_harness_all_cells.py -v
docker compose stop postgres valkey scylla opensearch
```

> **Cuidado com testes de integração contra Valkey:** `inverted:{context_id}`
> é uma chave GLOBAL compartilhada com dado real — teardown usa `SREM`
> (remove só membros de teste), nunca `DEL` na chave inteira.

Configuração de célula (`cells/*.yaml`, uma por célula viável) fecha a Etapa 5
— ver [docs/ARCHITECTURE.md#configuração-de-célula](docs/ARCHITECTURE.md#configuração-de-célula).

Depois de fechar a matriz de corretude, o catálogo item→contexto foi movido
para memória (correção de uma assimetria que penalizava Valkey/Scylla em E-1)
— mudança de arquitetura relevante, sem nenhuma latência medida para
motivá-la. Ver [docs/DECISIONS.md#fase-2-6](docs/DECISIONS.md#fase-2-6) para
o problema, a correção e a limitação registrada para a discussão de
resultados.

### Fase 3 — Camada de transmissão e ferramental (`service/`, `load/`, `analysis/`, `infra/`)

**Etapa 6 — `service/`**: `strategy`+`storage` viram um processo real
respondendo por HTTP/gRPC. Usa **Hypercorn**, não uvicorn (uvicorn não
implementa HTTP/2) — ver [docs/DECISIONS.md#etapa-6](docs/DECISIONS.md#etapa-6).

```
docker compose build tools service
docker compose up -d postgres
docker compose run --rm --entrypoint python tools schemas/postgres/load_oracle_fixture.py
docker compose up -d service
docker compose run --rm tools -m integration tests/acceptance/test_service_smoke.py -v
docker compose stop postgres service
```

**Etapa 7 — `load/`**: gerador de carga contra o `service`, em **k6**
(não Locust — modelo aberto de verdade, evita omissão coordenada; ver
[docs/DESIGN.md](docs/DESIGN.md#pilha)). Rodar localmente é só para
sanidade — [docs/DESIGN.md](docs/DESIGN.md#ambientes) proíbe medir latência
aqui.

```
docker compose up -d postgres
docker compose run --rm --entrypoint python tools schemas/postgres/load_oracle_fixture.py
docker compose up -d service
docker compose run --rm --entrypoint python tools load/export_contexts_by_tier.py
docker compose run --rm --entrypoint k6 tools run load/scenarios.js --vus 1 --duration 10s \
    -e CELL=e1-postgres -e TARGET_URL=http://service:8000/v1/recommendations
docker compose stop postgres service
```

**Etapa 8 — `analysis/`**: consolida a saída bruta do k6 e roda a estatística
exigida por [docs/DESIGN.md](docs/DESIGN.md#estatística) (Kruskal-Wallis,
Dunn/Bonferroni, bootstrap). Ver [docs/DECISIONS.md#etapa-8](docs/DECISIONS.md#etapa-8)
para o motivo de `analysis/collect.py` ler `requests.ndjson` em vez de uma
métrica customizada do k6.

```
docker compose run --rm tools -m "not integration" analysis/tests -v
```

**Etapa 9 — `infra/`** (Terraform, GCP): 4 módulos + 3 aplicações, validados
localmente sem credencial real. Nenhum `plan`/`apply` real acontece aqui —
isso é a Fase 4.

```
docker compose build tools
docker compose run --rm --entrypoint terraform tools fmt -check -recursive -diff infra/
for dir in bootstrap modules/network modules/database modules/service modules/loadgen modules/budget_killswitch envs/experiment envs/budget; do
  docker compose run --rm --entrypoint terraform tools -chdir=infra/$dir init -backend=false
  docker compose run --rm --entrypoint terraform tools -chdir=infra/$dir validate
done
```

### Fase 4 — Deploy em nuvem (GCP)

Passo a passo real, com credenciais e recursos **faturáveis** — cada `apply`
custa dinheiro e exige confirmação explícita antes de rodar. O Google
fornece um crédito de $300 para testar a plataforma. Roda em **Compute
Engine, não Cloud Run** — autoscaling/cold start de uma plataforma serverless
contaminariam a cauda de latência que o estudo mede.

**0. Pré-requisitos manuais**
- Criar um projeto GCP, vincular uma conta de faturamento, instalar o
  `gcloud` CLI e rodar `gcloud auth login`.
- Habilitar as APIs:

```
gcloud services enable \
  compute.googleapis.com storage.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com billingbudgets.googleapis.com iam.googleapis.com \
  iap.googleapis.com cloudresourcemanager.googleapis.com \
  cloudfunctions.googleapis.com run.googleapis.com eventarc.googleapis.com \
  pubsub.googleapis.com cloudbuild.googleapis.com cloudbilling.googleapis.com \
  --project=<project-id>
```

**1. Credenciais do Terraform** — sem gerar chave nenhuma, por impersonação:

```
infra/scripts/create_terraform_service_account.sh <project-id> <billing-account-id>
```

**2. Bootstrap** — cria os buckets de estado, resultados, função e dataset:

```
infra/scripts/with_terraform_credentials.sh <seu-projeto> -- \
  docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
  run --rm --entrypoint terraform tools -chdir=infra/bootstrap init
infra/scripts/with_terraform_credentials.sh <seu-projeto> -- \
  docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
  run --rm --entrypoint terraform tools -chdir=infra/bootstrap apply -var="project_id=<seu-projeto>"
```

Guarde os 4 outputs (`terraform_state_bucket`, `results_bucket`,
`function_source_bucket`, `dataset_bucket`) — usados nos passos seguintes.

**3. Orçamento** — antes de qualquer VM:

> **Pegadinha real:** `currency_code` precisa bater com a moeda da sua conta
> de faturamento (`gcloud billing accounts describe <conta>`), senão o
> `apply` falha com um "Error 400" genérico que não menciona moeda. Precisa
> ser passado de novo em **todo** apply separado — não é "sticky". Ver
> [docs/DECISIONS.md#fase-4](docs/DECISIONS.md#fase-4) para o histórico
> completo (inclui a trava de segurança de orçamento que desliga o billing a
> 120% de gasto).

```
infra/scripts/with_terraform_credentials.sh <seu-projeto> -- \
  docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
  run --rm --entrypoint terraform tools -chdir=infra/envs/budget \
  init -backend-config="bucket=<terraform_state_bucket>"
infra/scripts/with_terraform_credentials.sh <seu-projeto> -- \
  docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
  run --rm --entrypoint terraform tools -chdir=infra/envs/budget apply \
  -var="project_id=<seu-projeto>" -var="billing_account_id=<sua-conta>" -var="monthly_budget_usd=<valor>" \
  -var="currency_code=<moeda-da-sua-conta>" \
  -var="function_source_bucket=<function_source_bucket>"
```

**4. Imagens** — build e push para o Artifact Registry (fora do Terraform,
com sua sessão pessoal):

```
infra/scripts/build_and_push_images.sh <project-id> <region> [tag]
```

**5. Secret Manager** — senha do Postgres (passo manual, sem recurso Terraform):

```
echo -n "<senha-real>" | gcloud secrets create tcc-postgres-password --data-file=- --project=<seu-projeto>
```

**6. `terraform.tfvars`** — preencher `service_image`/`tools_image` (do
passo 4); `project_id`/`cell`/`storage` aqui servem só de referência, os
scripts sobrescrevem via `-var` a cada chamada:

```
cd infra/envs/experiment
cp terraform.tfvars.example terraform.tfvars   # nunca commitar
```

**7. Smoke test em nuvem** — antes de qualquer bateria real, para cada banco:

```
export TOOLS_IMAGE=us-east4-docker.pkg.dev/<seu-projeto>/tcc/tools:latest
python infra/scripts/cloud_smoke_test.py e1-postgres <project-id> us-east4 us-east4-a <terraform_state_bucket> <dataset_bucket> <results_bucket>
python infra/scripts/cloud_smoke_test.py e1-valkey <project-id> us-east4 us-east4-a <terraform_state_bucket> <dataset_bucket> <results_bucket>
python infra/scripts/cloud_smoke_test.py e1-scylla <project-id> us-east4 us-east4-a <terraform_state_bucket> <dataset_bucket> <results_bucket>
python infra/scripts/cloud_smoke_test.py e1-opensearch <project-id> us-east4 us-east4-a <terraform_state_bucket> <dataset_bucket> <results_bucket>
```

**Só avance para a Fase 5 depois que o smoke test passar limpo** para a
célula em questão. `--keep-infra` pula o destroy final, para investigar uma
falha manualmente.

### Fase 5 — Execução dos testes de carga e captura de resultados

> **Antes de interpretar qualquer resultado, leia [docs/BENCHMARKS.md](docs/BENCHMARKS.md).**
> Traz a expectativa de latência célula a célula e a régua de diagnóstico
> que separa saturação de latência real.

**0. Massa de dados completa** — o smoke test carrega só o subconjunto do
oráculo; para medir de verdade é preciso a base real (U=200.948):

```
docker compose run --rm generator all --seed 42   # sem --sample-users
infra/scripts/with_terraform_credentials.sh <seu-projeto> -- \
  docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
  run --rm --entrypoint terraform tools -chdir=infra/bootstrap init
infra/scripts/with_terraform_credentials.sh <seu-projeto> -- \
  docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
  run --rm --entrypoint terraform tools -chdir=infra/bootstrap apply -var="project_id=<seu-projeto>"
infra/scripts/upload_dataset.sh <dataset_bucket>
infra/scripts/build_and_push_images.sh <project-id> <region>   # reconstrói tools com os loaders novos
```

**1. Semear o dataset uma vez por banco** (opcional, mas recomendado — evita
recarregar a base completa a cada `apply`/`destroy`):

```
export TOOLS_IMAGE=us-east4-docker.pkg.dev/<seu-projeto>/tcc/tools:latest
python -m infra.scripts.seed_dataset_snapshots postgres <project-id> us-east4 us-east4-a \
    <terraform_state_bucket> <dataset_bucket> <results_bucket>
python -m infra.scripts.seed_dataset_snapshots scylla <project-id> us-east4 us-east4-a \
    <terraform_state_bucket> <dataset_bucket> <results_bucket>
python -m infra.scripts.seed_dataset_snapshots opensearch <project-id> us-east4 us-east4-a \
    <terraform_state_bucket> <dataset_bucket> <results_bucket>
```

> **Mudou o schema? Refaça o snapshot** do storage afetado antes de medir —
> `run_measurement_battery.py` pula schema **e** carga quando o snapshot
> existe. Valkey fica de fora (100% em memória, sem disco). E antes de
> refazer o snapshot, confirme que `tools:latest`/`service:latest` já foram
> republicados com o fix — regenerar o snapshot com uma imagem
> desatualizada reproduz o mesmo problema no disco restaurado. Ver
> [docs/BENCHMARKS.md#10-a-armadilha-da-imagem-desatualizada](docs/BENCHMARKS.md#10-a-armadilha-da-imagem-desatualizada)
> antes de confiar em qualquer bateria real após um fix de schema/estratégia.

**Triagem** — todas as 14 células, carga/seletividade fixas + rampa curta de
saturação. Rode a primeira com `--verify-otel` (confirma que a instrumentação
de recursos está exportando métricas — vale para as 14, não repita):

```
export TOOLS_IMAGE=us-east4-docker.pkg.dev/<seu-projeto>/tcc/tools:latest
python -m infra.scripts.run_measurement_battery e1-postgres <project-id> us-east4 us-east4-a \
    <terraform_state_bucket> <results_bucket> <dataset_bucket> --phase triagem --verify-otel
```

Demais 13 células (troque só o nome), via script ou `make`:

```
python -m infra.scripts.run_measurement_battery e1-valkey <project-id> us-east4 us-east4-a \
    <terraform_state_bucket> <results_bucket> <dataset_bucket> --phase triagem
# ou:
make saturation-triagem CELL=e1-postgres PROJECT_ID=<project-id> TF_STATE_BUCKET=<terraform_state_bucket> \
    RESULTS_BUCKET=<results_bucket> DATASET_BUCKET=<dataset_bucket> TOOLS_IMAGE=us-east4-docker.pkg.dev/<seu-projeto>/tcc/tools:latest
```

`--keep-infra` pula o destroy para investigar uma falha manualmente. Se a
rampa reportar `loadgen_bottleneck` (gerador saturou antes da célula), escale
a VM do gerador e repita só a rampa. `--yes` pula a confirmação interativa
"sim" de apply/destroy — o aviso `FATURÁVEL` continua sendo impresso, só não
bloqueia em `input()`; use apenas em execução supervisionada (célula por
célula, com aprovação já dada fora do comando), nunca como default. Consolide
e ache a fronteira de Pareto:

```
docker compose run --rm --entrypoint python tools analysis/report.py \
    results --phase triagem --out results/report/triagem
```

A fronteira é 2D (latência × custo(D)) e **depende da demanda**: use
`pareto_frontier_union` de `report.json`, a união das fronteiras sobre todo o
domínio — é o conjunto que segue para a confirmação, conservador de propósito
(não descarta célula que vença só em alguma faixa). `frontier_segments` e
`crossovers` mostram em que demanda a decisão muda.

Leia `saturation_throughput_approx` (ou `saturation_lower_bound`, se
censurada) de cada célula dessa união — é o `--saturation-start` do próximo
passo.

Para re-medir só a vazão de saturação (por exemplo, depois de mudar a
metodologia da rampa) sem descartar as repetições de latência já coletadas,
que são a parte cara, use `--only-saturation`: ele pula a bateria de carga
fixa e grava um `saturation.json` novo, que `analysis/report.py` encontra
varrendo a árvore de resultados.

**Confirmação** — só as células da fronteira, varredura completa de carga ×
seletividade + 5 repetições, mais a rampa fina de saturação:

```
python -m infra.scripts.run_measurement_battery <cell-da-fronteira> <project-id> us-east4 us-east4-a \
    <terraform_state_bucket> <results_bucket> <dataset_bucket> --phase confirmacao \
    --saturation-start <valor-do-report.json-da-triagem>
# ou:
make saturation-confirmacao CELL=<cell-da-fronteira> START=<valor-do-report.json-da-triagem> \
    PROJECT_ID=<project-id> TF_STATE_BUCKET=<terraform_state_bucket> RESULTS_BUCKET=<results_bucket> \
    DATASET_BUCKET=<dataset_bucket> TOOLS_IMAGE=us-east4-docker.pkg.dev/<seu-projeto>/tcc/tools:latest

docker compose run --rm --entrypoint python tools analysis/report.py \
    results --phase confirmacao --out results/report/confirmacao
```

**Extraindo e lendo os resultados.** Cada execução grava em
`results/<cell>/<phase>/<timestamp>/` (local, sincronizado com o bucket de
resultados) — ver [docs/ARCHITECTURE.md#coleta-de-resultados](docs/ARCHITECTURE.md#coleta-de-resultados)
para o formato completo. Para uma leitura rápida sem processar nada, abra
`saturation.json` e `rep<N>/manifest.json` diretamente (JSON pequenos);
`requests.ndjson` nunca deve ser lido à mão — é insumo de `analysis/collect.py`,
chamado automaticamente por `analysis/report.py` acima. Para baixar de outra
máquina: `gcloud storage cp --recursive gs://<results_bucket>/<cell>/ results/<cell>/`.

> `generator_cpu_unmeasured: true` significa telemetria sem leitura (não
> "gerador ocioso") — ver [docs/DECISIONS.md#fase-5](docs/DECISIONS.md#fase-5)
> antes de confiar em medições arquivadas antes desta distinção existir.

As 14 células rodam sobre **hardware idêntico** — ver a tabela completa em
[docs/DESIGN.md#equivalência-de-infraestrutura-entre-células](docs/DESIGN.md#equivalência-de-infraestrutura-entre-células).
