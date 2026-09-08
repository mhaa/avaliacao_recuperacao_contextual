# Arquitetura do código — especificação e práticas

Complementa [DESIGN.md](DESIGN.md) (desenho experimental) e
[`data_generation/README.md`](../data_generation/README.md) (geração de massa
de dados). Este arquivo cobre a arquitetura do código, o ambiente local em
Docker e o provisionamento em nuvem com Terraform.

## Princípio que governa todas as decisões

O experimento compara catorze células (estratégia × banco). Se cada célula tiver
seu próprio código de infraestrutura, uma diferença de latência pode vir da
estratégia, do banco **ou** de uma implementação acidentalmente diferente. Isso é
confundimento de fatores e invalida o resultado.

Portanto: **um único serviço, um único binário, uma única imagem de contêiner.**
A célula é escolhida por configuração em tempo de execução (`CELL=e4-valkey`).
Nada de um diretório ou uma imagem por célula.

Tudo que difere entre células vive em dois lugares — `strategies/` e `storage/` —
e em nenhum outro.

---

## Estrutura do repositório

```
.
├── README.md                    # ponto de entrada
├── docs/                        # este arquivo, DESIGN.md, DECISIONS.md, BENCHMARKS.md
├── pyproject.toml
├── Makefile                     # atalhos; ver seção "Interface"
├── docker-compose.yml           # ambiente local (Postgres/Valkey/Scylla/OpenSearch)
├── docker-compose.gcp.yml       # override para rodar Terraform via o container tools
├── docker/
│   ├── Dockerfile.service       # imagem final slim, usuário não-root
│   └── Dockerfile.tools         # Python + k6 + clientes de banco + Terraform + libs de análise
│
├── data_generation/             # gerador de massa (ver data_generation/README.md)
│   └── generator/
│
├── core/                        # compartilhado por TODAS as células
│   ├── contract.py              # Candidate, Request, Response, ResponseItem
│   ├── ordering.py              # regra de ordenação e desempate
│   ├── session.py               # exclusão de itens da sessão
│   ├── config.py                # carga e validação de configuração de célula (Pydantic)
│   ├── registry.py              # resolve strategy/storage de cells/*.yaml para classes reais
│   └── catalog.py                # catálogo item->contexto residente em memória
│
├── storage/                     # um adaptador por banco
│   ├── base.py                  # StorageAdapter + primitivas + PrimitiveNotSupported
│   ├── postgres.py
│   ├── valkey.py
│   ├── scylla.py
│   ├── opensearch.py
│   └── tests/fakes.py           # adaptador fake em memória, usado sem banco real
│
├── strategies/                  # uma implementação por estratégia
│   ├── base.py                  # Strategy Protocol, check_compatibility, build_cell_runtime
│   ├── e1_app_filter.py
│   ├── e2_pushdown.py
│   ├── e3_prematerialized.py
│   └── e4_intersection.py
│
├── service/
│   ├── http_app.py               # FastAPI via Hypercorn; serve T-A (HTTP/1.1) e T-B (HTTP/2)
│   ├── grpc_app.py                # T-C
│   ├── main.py                    # monta a célula a partir de CELL=<id>
│   └── proto/recommendation.proto
│
├── schemas/                     # DDL e carga, por banco
│   ├── postgres/                 # 001_schema.sql, 002_prematerialized.sql, apply_schema.py, load_*.py
│   ├── valkey/
│   ├── scylla/
│   └── opensearch/
│
├── harness/                     # verificação de equivalência funcional
│   ├── oracle.py                 # carrega data_generation/data/oracle.parquet
│   ├── fixtures.py               # carrega o subconjunto de usuários referenciado pelo oráculo
│   ├── verify.py                 # compara resposta de uma célula contra o oráculo
│   └── verify_cli.py             # usado pelo smoke test em nuvem como gate de corretude
│
├── tests/acceptance/            # testes de ponta a ponta, cruzando módulos
│   ├── test_harness_all_cells.py               # VIABLE_CELLS x 1000 casos do oráculo
│   ├── test_infeasible_cells_fail_at_startup.py # as 2 células inviáveis falham na montagem
│   └── test_service_smoke.py                   # amostra via HTTP real
│
├── load/                        # scripts de carga
│   ├── scenarios.js              # k6, executor constant-arrival-rate / ramping-arrival-rate
│   ├── zipf.js                   # sorteio de user_id (CDF pré-computada)
│   ├── saturation.py             # rampa de busca de vazão de saturação
│   ├── run_battery.py            # roda scenarios.js N vezes por célula, grava em results/
│   └── export_contexts_by_tier.py
│
├── analysis/
│   ├── collect.py                # requests.ndjson -> latencies.parquet
│   ├── report.py                 # agrega, roda estatística, gera report.json + gráficos
│   ├── stats.py                  # Kruskal-Wallis, Dunn/Bonferroni, bootstrap, TOST
│   ├── pareto.py                 # fronteira de Pareto (latência x custo x vazão de saturação)
│   ├── resources.py              # CPU/memória/rede — DockerStatsCollector (local), GCPMonitoringCollector
│   ├── storage_size.py           # volume ocupado em disco, por banco
│   └── plots.py
│
├── cells/                       # UMA configuração por célula
│   ├── _defaults.yaml            # cache, transport, params (n_candidates, k, prematerialized_m, exclusion_size)
│   └── e{1,2,3,4}-{postgres,valkey,scylla,opensearch}.yaml   # 14 arquivos viáveis
│
├── infra/                       # Terraform
│   ├── bootstrap/                 # bucket de estado + resultados + function source + dataset
│   ├── modules/{network,database,service,loadgen,budget_killswitch}/
│   ├── envs/{experiment,budget,seed}/
│   └── scripts/                   # with_terraform_credentials.sh, run_measurement_battery.py, ...
│
└── results/                     # saída das execuções (git-ignored exceto .gitkeep)
    └── <cell-id>/<phase>/<timestamp>/
```

---

## Abstrações centrais

### `storage/base.py`

Define as primitivas que cada banco pode ou não oferecer — é aqui que a matriz
de viabilidade ([DESIGN.md](DESIGN.md)) fica codificada:

```python
GET_CANDIDATES = "get_candidates"
GET_CANDIDATES_FILTERED = "get_candidates_filtered"
GET_PREMATERIALIZED = "get_prematerialized"
INTERSECT = "intersect"
LOAD_ITEM_CONTEXTS = "load_item_contexts"   # primitiva de carga, não de requisição

class StorageAdapter:
    name: str = "unknown"
    supported_primitives: frozenset[str] = frozenset()

    async def get_candidates(self, user_id: int) -> list[Candidate]: ...
    async def get_candidates_filtered(self, user_id: int, context: list[int]) -> list[Candidate]: ...
    async def get_prematerialized(self, user_id: int, context_key: str) -> list[Candidate]: ...
    async def intersect(self, user_id: int, context: list[int], limit: int) -> list[Candidate]: ...
    async def load_item_contexts(self) -> dict[int, frozenset[int]]: ...
```

`Candidate` carrega só `(item_id, score)` — a pertença item→contexto é dado de
catálogo, carregada uma vez via `load_item_contexts` e mantida em memória
(`core/catalog.py`), nunca reconstruída por requisição (ver
[DESIGN.md](DESIGN.md#catálogo-itemcontexto-residente-na-aplicação-decisão-de-e-1e-3)
e [DECISIONS.md](DECISIONS.md#fase-2-6) para o porquê).

Por padrão, cada método levanta `PrimitiveNotSupported` — um adaptador só
sobrescreve o que de fato implementa. `scylla.py` não implementa `intersect`
(E-4 é inviável em BD-3, sem primitiva de interseção de conjuntos em CQL);
`opensearch.py` não implementa `get_prematerialized` (E-3 não tem sentido
arquitetural sobre um índice invertido). Um adaptador que não suporta uma
primitiva falha na **montagem** da célula, nunca em tempo de requisição.

### `strategies/base.py`

```python
class Strategy(Protocol):
    name: str
    required_primitives: frozenset[str]
    async def prepare(self, storage: StorageAdapter) -> None: ...
    async def retrieve(self, storage: StorageAdapter, req: Request) -> Response: ...

def check_compatibility(strategy: Strategy, storage: StorageAdapter) -> None: ...
async def build_cell_runtime(strategy: Strategy, storage: StorageAdapter) -> None:
    check_compatibility(strategy, storage)
    await strategy.prepare(storage)
```

`check_compatibility` compara `required_primitives` contra
`storage.supported_primitives` na montagem da célula — incompatibilidade
levanta `PrimitiveNotSupported`, nomeando a primitiva ausente, antes de
qualquer requisição.

`prepare` é o gancho de montagem assíncrona: hoje só E-1 e E-3 usam (carregar
o catálogo item→contexto de `core/catalog.py`); E-2 e E-4 delegam o predicado
ao banco e não precisam de nada. Existe porque a montagem da célula
(`core/registry.py`) é síncrona e não pode fazer I/O. `build_cell_runtime` é o
ponto único chamado pelos quatro donos de event loop que montam uma célula:
`service/http_app.py` (lifespan), `service/main.py`, `harness/verify_cli.py` e
os testes de aceitação — sem isso, esquecer `prepare` num deles apareceria só
como E-1 devolvendo resposta vazia em tempo de requisição.

### Invariantes que valem para todas as células

Implementadas em `core/`, nunca duplicadas dentro de uma estratégia:

- Ordenação: `rank` crescente; empate resolvido por `item_id` crescente
  (`core/ordering.py`).
- Exclusão de sessão: aplicada na camada de aplicação em **todas** as
  estratégias, inclusive E-3 e E-4 — nenhuma estratégia empurra a exclusão
  para o banco, para manter a variação restrita ao predicado categórico
  (`core/session.py`).
- Resposta incompleta: se não houver itens elegíveis suficientes, retorna os
  que houver, nunca completando de outra fonte; `returned_count` registra a
  contagem real (`core/contract.py:build_response`).
- Sem metadados descritivos na resposta — só `item_id`, `score`, `rank`.

---

## Configuração de célula

`cells/e4-valkey.yaml`:

```yaml
id: e4-valkey
strategy: e4_intersection
storage: valkey
storage_config:
  host: valkey
  port: 6379
```

`cells/_defaults.yaml` guarda o que é comum a todas (`cache`, `transport`,
`params` — `n_candidates`, `k`, `prematerialized_m`, `exclusion_size`, os
parâmetros fixos de [DESIGN.md](DESIGN.md)); cada `cells/<id>.yaml` sobrepõe
só `id`, `strategy`, `storage` e `storage_config`. Validado com Pydantic e
`extra="forbid"` em todo nível — um campo desconhecido (erro de digitação)
levanta erro em vez de virar configuração padrão silenciosamente.

Seleção em runtime: `CELL=e4-valkey python -m service.main`. `core/registry.py`
resolve as strings `strategy`/`storage` para as classes reais de
`strategies/`/`storage/`.

---

## Ambiente local — Docker

Requisito: nenhuma dependência instalada no host além de Docker e Docker
Compose. Nem Python, nem k6, nem clientes de banco — tudo roda no container
`tools` (que empacota Python, k6, clientes de banco, Terraform e as
bibliotecas de análise) ou `service`.

### Regras

- `docker/Dockerfile.tools` é a imagem usada para gerar massa, carregar
  esquemas, rodar o arnês, disparar carga e analisar resultados.
- `docker/Dockerfile.service` é multi-stage, imagem final slim, usuário
  não-root, sem ferramentas de build.
- Healthchecks nos bancos; `service` depende de `condition: service_healthy`.
- Rede dedicada do compose — o serviço alcança os bancos por nome DNS.

### Cuidados por banco (não descobrir isso na tentativa e erro)

**ScyllaDB** exige `--smp 1 --memory 2G --overprovisioned 1 --developer-mode 1`
localmente. Sem isso tenta consumir a máquina inteira. (Na nuvem, a
configuração é bem diferente — ver [DESIGN.md](DESIGN.md#equivalência-de-infraestrutura-entre-células).)

**OpenSearch** exige `vm.max_map_count=262144` no host (`sudo sysctl -w
vm.max_map_count=262144`).

**Valkey** com `--maxmemory-policy noeviction` — despejo silencioso falsearia
os resultados sem gerar erro.

**PostgreSQL** com `shared_buffers`/`work_mem` explícitos.

### Escala local

Local é para correção, nunca para medição — `--sample-users 10000`. Números de
latência medidos localmente não significam nada (ambientes não são
comparáveis — ver [DESIGN.md](DESIGN.md#ambientes)).

---

## Ambiente de nuvem — Terraform

### Práticas obrigatórias

- **Backend remoto com bloqueio de estado** (bucket GCS com versionamento).
  Nunca estado local (única exceção documentada: `infra/bootstrap/`, porque o
  bucket de estado ainda não existe quando ele roda).
- **Módulos com responsabilidade única**: `network`, `database`, `service`,
  `loadgen`, `budget_killswitch`. Nenhum recurso solto em `envs/`.
- **Versões travadas**: `required_version` do Terraform e `version` de cada
  provider, com `.terraform.lock.hcl` **versionado** no repositório (ao
  contrário de `.terraform/`, cache de provedores, e `*.tfstate*`, ambos no
  `.gitignore`).
- **Nenhum segredo em código ou em `.tfvars` versionado.** Usar Secret
  Manager e referenciar. `terraform.tfvars` no `.gitignore`;
  `terraform.tfvars.example` versionado com placeholders.
- **Variáveis com `type`, `description` e `validation`** onde couber
  (`cell` só aceita uma das 14 células viáveis; `storage`, só os 4 bancos
  reais). Outputs com `description`.
- **Tags/labels em todo recurso**: `project`, `cell`, `phase`,
  `managed_by=terraform` — permite atribuir custo por célula depois.
- **`prevent_destroy` só no bucket de resultados.** No resto, não.
- Sem `local-exec` para lógica de aplicação. Provisionamento é Terraform;
  execução é script separado (`infra/scripts/`).

### Topologia

Três VMs por medição, em zona única, sub-rede privada (ver a tabela de
hardware idêntico em [DESIGN.md](DESIGN.md#equivalência-de-infraestrutura-entre-células)):
banco (`n2-standard-8`), serviço (`n2-standard-8`), gerador de carga
(`n2-standard-8`, **obrigatoriamente separado** — a metodologia exige
verificar que a CPU do gerador ficou abaixo de 60%, para descartar que ele
seja o gargalo). Sem IP público nas VMs de banco e serviço; acesso por IAP.

### Custo

Carga completa idêntica entre todas as células de uma mesma tecnologia de
banco — `infra/scripts/seed_dataset_snapshots.py` semeia uma vez por storage
e tira um snapshot de nome fixo (`tcc-dataset-seed-<storage>`);
`run_measurement_battery.py` detecta esse snapshot automaticamente e pula a
carga quando ele existe. Valkey fica de fora — roda 100% em memória, sem
disco persistente. VMs Spot para carga/depuração; **nunca** para medição
(interrupção e desempenho variável contaminariam a cauda). Alerta de
orçamento (com trava de segurança que desliga o billing a 120% de gasto)
configurado **antes** da primeira VM.

---

## Coleta de resultados

`results/<cell>/<phase>/<timestamp>/`, sincronizado com
`gs://<results_bucket>/<cell>/<phase>/<timestamp>/`:

```
results/<cell>/<phase>/<timestamp>/
├── saturation.json          # só na triagem — vazão de saturação (rampa curta)
├── saturation_<tier>.json   # só na confirmação, um por seletividade
├── resources.csv            # só na confirmação — CPU/memória/rede das 3 VMs a cada 5s
└── rep<N>/
    ├── manifest.json         # célula, taxa, seletividade, região/zona, timestamp, hash do commit
    ├── requests.ndjson       # uma linha JSON por requisição (console.log de load/scenarios.js)
    ├── k6-raw.json           # dump nativo do k6 (--out json=) — só diagnóstico
    ├── latencies.parquet     # gerado por analysis/collect.py a partir de requests.ndjson
    └── summary.json          # percentis/vazão/erro, calculado em Python sobre latencies.parquet
```

`manifest.json` inclui o **hash do commit** — se a implementação mudar no
meio de uma bateria, é o que permite saber quais medições vieram de qual
versão. `requests.ndjson` nunca deve ser lido à mão (centenas de milhares de
linhas numa bateria real); é insumo de `analysis/collect.py`, chamado
automaticamente por `analysis/report.py`. `k6-raw.json` é só a saída nativa
de diagnóstico do próprio k6 — nada no pipeline o lê.

`analysis/report.py results --phase <triagem|confirmacao> --out
results/report/<phase>` agrega as repetições ainda não coletadas por célula,
roda Kruskal-Wallis → Dunn (Bonferroni) → epsilon-quadrado → IC de bootstrap
do p99, e escreve `report.json` — com `cost_model`, `demand_levels`,
`frontier_segments`, `crossovers`, `pareto_frontier_union` e
`censorship_warning` — mais um `pareto_D<nível>.png` por nível de demanda e
`custo_vs_demanda.png`. A fronteira é 2D (latência × custo(D)) e depende da
demanda, daí `pareto_frontier_union` (a união sobre o domínio) ser o conjunto
que segue para a confirmação. Nela, também roda TOST par-a-par entre as
células da fronteira.

Guardar latências individuais, não só percentis agregados — o bootstrap e o
TOST precisam da distribuição completa.

---

## Interface

`Makefile` como fachada; por baixo, sempre Docker/`docker compose`/`python -m`
— nenhuma lógica nova, cada alvo só encaminha:

```
make gen-data SAMPLE=10000       # gera massa
make up DB=postgres              # sobe um banco
make load-schema CELL=e1-postgres
make verify CELL=e1-postgres     # arnês; obrigatório antes de medir
make verify-all                  # todas as 14 células
make smoke                       # carga curta local, sanidade
make down
make analyze
```

Nuvem:

```
make tf-plan CELL=e1-postgres PROJECT_ID=<id>
make tf-apply CELL=e1-postgres PROJECT_ID=<id>
make measure CELL=e1-postgres PHASE=triagem PROJECT_ID=<id> ...
make tf-destroy CELL=e1-postgres PROJECT_ID=<id>
```

Vazão de saturação ([DESIGN.md](DESIGN.md#vazão-de-saturação-3ª-dimensão-da-fronteira-de-pareto)),
alvos nomeados, equivalentes a `make measure` com a fase certa:

```
make saturation-triagem CELL=e1-postgres ...          # rampa curta, dentro da triagem
make saturation-confirmacao CELL=e1-postgres START=11000 ...  # rampa fina, só na fronteira
```

---

## Ordem de implementação (histórica, já concluída para a Fase 1)

1. `core/` — contrato, ordenação, exclusão de sessão. Testes unitários primeiro.
2. `storage/postgres.py` + `strategies/e1_app_filter.py` — a célula mais simples,
   ponta a ponta.
3. `harness/` — oráculo e verificação. **Antes** de qualquer outra célula.
4. Demais estratégias sobre PostgreSQL, verificando cada uma contra o oráculo.
5. Demais adaptadores de banco (Valkey, Scylla, OpenSearch).
6. `service/`, depois `load/` com k6.
7. `analysis/`.
8. `infra/`.

O passo 3 não é negociável e não pode ser adiado. Sem o oráculo, implementar
treze células sem saber se alguma está errada é possível — e uma implementação
que resolve o predicado de forma incompleta parece mais rápida justamente por
estar errada. Para a narrativa completa de cada etapa (decisões tomadas, bugs
reais e como foram corrigidos), ver [DECISIONS.md](DECISIONS.md).

---

## Não fazer

- Um diretório, uma imagem ou um serviço por célula.
- Copiar código de `core/` para dentro de uma estratégia.
- Empurrar a exclusão de sessão para o banco em qualquer célula.
- Medir latência no ambiente local.
- Medir qualquer célula que não tenha passado no arnês.
- Estado do Terraform em disco local (exceto `infra/bootstrap/`, por necessidade).
- Segredos em arquivo versionado.
- Rodar medição em VM Spot.
- Ajustar parâmetros do ALS para melhorar recomendações — não é variável do estudo.
