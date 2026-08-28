# TCC — Arquitetura de bancos de dados para recuperação em sistemas de recomendação

Projeto de pesquisa (TCC) que investiga arquiteturas de banco de dados para a etapa
de **recuperação** de dados em sistemas de recomendação. Serão avaliadas 4 estratégias
nomeadas E-1...E-4, aplicadas a 4 paradigmas de armazenamento, nomeados BD-1...BD-4.
Cada combinação de estratégia e banco de dados é referenciada neste trabalho como "célula"
e são 14 no total (4 estratégias x 4 bancos - 2 combinações inviáveis)

O detalhes do experimento em [CONTEXTO.md](CONTEXTO.md), a fonte verdade do projeto.

## Fases do projeto em ordem de produção

Desenvolvido em 3 fases representando as 3 camadas da aplicação:
- Fase 1 - Preparação da massa de dados offline
- Fase 2 - Implementação dos serviços de recuperação (células)
- Fase 3 - Implementação da camada de transmissão (APIs) que irá externalizar o serviço de recuperação para o teste de carga
- Fase 4 - Deploy em nuvem GCP
- Fase 5 - Execução dos testes de carga e  captura de resultados

### Fase 1 — Preparação da massa de dados

Não é escopo deste projeto a modelagem de aprendizado de máquina para geração de
recomendações. Será utilizado a base existente do MovieLens 32M com algoritmo ALS.
Scripts de geração e preparação da massa de dados utilizada nos experimentos estão
na pasta [`data_generation/`](data_generation/).

Execução via container diretamente na raiz do repositório, com download automatico 
da base MovieLens 32M e saída armazenada em [`data_generation/data`](data_generation/data):

```
docker compose build generator
docker compose run --rm generator all --sample-users 10000 --seed 42
```

Este exemplo roda a massa de dados em escala de desenvolvimento (10.000 usuários), 
para gerar a base real completa (~200 mil usuários) basta omitir `--sample-users`.

### Fase 2 — Serviço de recuperação (`core/`, `storage/`, `strategies/`, ...)

Implementação das 14 células de recuperação, 4 estratégias (E-1..E-4)
sobre os quatro bancos (BD-1..BD-4) (eliminados 2 casos inviaveis). 
Para rodar localmente os unitários desta fase sem banco de dados (adpatador fake): 

```
docker compose build tools
docker compose run --rm tools         # camada rápida, sem banco
```

#### Fase 2.1 — `core/`

Define regras e contratos que devem ser compartilhadas por **todas** as 14 
configurações de teste (células), tais como: ordenação com desempate (`rank`
crescente, `item_id` crescente em empate), exclusão de itens da sessão, o
contrato de requisição/resposta (`core/contract.py`) e a montagem de
respostas incompletas (nunca completadas de outra fonte).

#### Fase 2.2 — `storage/postgres.py` + `strategies/e1_app_filter.py`

Primeira célula ponta a ponta (a mais simples da matriz 4×4): E-1 (filtro na
aplicação) sobre Postgres (BD-1). 

`storage/base.py` define as primitivas que
cada banco pode ou não oferecer (`get_candidates`, `get_candidates_filtered`,
`get_prematerialized`, `intersect`) — um adaptador que não suporta uma
primitiva falha na **montagem** da célula, nunca em tempo de requisição.

`storage/postgres.py` implementa `get_candidates`/`get_candidates_filtered`
via `psycopg` (assíncrono); 

`storage/tests/fakes.py` traz um adaptador fake
em memória com as 4 primitivas, usado pelos testes de estratégia sem precisar
de banco algum. O esquema mínimo do Postgres está em
[`schemas/postgres/`](schemas/postgres/).

Como rodar:

```
docker compose build tools

# camada rápida (core/, storage/ com fake, strategies/) — sem banco
docker compose run --rm tools

# camada de integração — exige Postgres real
docker compose up -d postgres
docker compose run --rm tools -m integration storage/tests/test_postgres_adapter.py -v
docker compose stop postgres
```

> Atenção: argumentos passados a `docker compose run --rm tools <args>`
> **substituem** o `CMD` padrão da imagem, não se somam a ele. Para rodar só
> a camada rápida com saída verbosa, use
> `docker compose run --rm tools -m "not integration" -v`.

#### Etapa 3 — `harness/` (oráculo + verificação) — obrigatória antes de qualquer outra célula

Compara a resposta de uma célula contra `data_generation/data/oracle.parquet`
(1000 casos com resultado esperado, computado de forma independente por
`data_generation/generator/oracle.py`). `harness/oracle.py` carrega os casos;
`harness/verify.py` compara item a item (ordem importa; resposta curta que
bate com o oráculo não é suspeita — 649 dos 1000 casos são assim, de
propósito; score compara com tolerância de ponto flutuante) e produz um
relatório legível por máquina. `tests/acceptance/test_harness_all_cells.py`
roda essa comparação, com os 1000 casos, contra cada célula viável já
implementada (ver Etapa 4 abaixo para a lista atual); a lista cresce na
Etapa 5, sem mudar o teste. Nenhuma célula é considerada pronta, e nenhuma
latência pode ser medida, sem passar 100% aqui (ver `CONTEXTO.md`, "regra de
ouro da implementação").

Como o teste de ponta a ponta precisa de dado real equivalente ao que gerou
o oráculo, `schemas/postgres/load_oracle_fixture.py` carrega em uma Postgres
já com o esquema aplicado só os candidatos e pertences item→contexto dos
usuários referenciados pelos 1000 casos (não a base inteira).

Como rodar:

```
docker compose build tools
docker compose run --rm tools -m "not integration" -v   # camada rápida

# camada de integração — exige Postgres real + massa gerada
# (data_generation/data/oracle.parquet etc.; ver Fase 1 se ainda não gerou)
docker compose up -d postgres
docker compose run --rm --entrypoint python tools schemas/postgres/load_oracle_fixture.py
docker compose run --rm tools -m integration -v
docker compose stop postgres
```

> Cuidado ao escrever novos testes de integração contra Postgres: nunca
> hardcode `item_id`s "de teste" dentro da faixa real do catálogo (denso,
> `0..87584`) — um teardown que faz `DELETE ... WHERE item_id = ANY(...)`
> apagaria dado real carregado por `load_oracle_fixture.py` sem erro
> visível, só corrompendo silenciosamente outro teste rodado depois na
> mesma sessão de banco (foi exatamente o bug encontrado e corrigido ao
> validar esta etapa — ver `storage/tests/test_postgres_adapter.py`).

#### Etapa 4 — E-2, E-3, E-4 sobre Postgres, cada uma verificada contra o oráculo

Completa a primeira linha da matriz 4×4 (as 4 estratégias, ainda só sobre
BD-1). `strategies/e2_pushdown.py` delega o predicado ao banco
(`get_candidates_filtered`). `strategies/e4_intersection.py` intersecta os
candidatos do usuário com a(s) lista(s) invertida(s) do(s) contexto(s)
pedido(s) (`intersect`) — como as listas invertidas não são truncadas, isso
já funciona corretamente mesmo para contexto composto (dois `context_id` em
AND).

`strategies/e3_prematerialized.py` tem uma decisão de implementação que vale
registrar: `prematerialized.parquet` guarda só o top-40 por (usuário,
contexto ÚNICO) — intersectar dois top-40 (um por contexto) não garante o
mesmo resultado do oráculo, que filtra sobre os 500 candidatos completos.
Por isso E-3 só usa a leitura pré-materializada quando a requisição pede um
único contexto; para contexto composto, cai para leitura completa + filtro
em aplicação (mesmo caminho de E-1). Isso é um resultado citável do TCC (E-3
não tem vantagem em contexto composto), não um bug — ver a docstring do
módulo para os detalhes.

Todas as 4 estratégias reaproveitam a mesma suíte de verificação da Etapa 3;
`VIABLE_CELLS` em `tests/acceptance/test_harness_all_cells.py` agora tem
`e1-postgres`, `e2-postgres`, `e3-postgres`, `e4-postgres`.
`schemas/postgres/load_oracle_fixture.py` também carrega, além de
candidatos e pertences item→contexto, as linhas pré-materializadas
(`schemas/postgres/002_prematerialized.sql`) dos usuários referenciados
pelo oráculo.

Como rodar:

```
docker compose build tools
docker compose run --rm tools -m "not integration" -v   # camada rápida

# camada de integração — exige Postgres real + massa gerada
docker compose up -d postgres
# se o volume do Postgres já existir de uma etapa anterior sem a tabela nova:
docker exec -i tcc-postgres psql -U tcc -d recsys < schemas/postgres/002_prematerialized.sql
docker compose run --rm --entrypoint python tools schemas/postgres/load_oracle_fixture.py
docker compose run --rm tools -m integration tests/acceptance/test_harness_all_cells.py -v
docker compose stop postgres
```

A lógica de carregamento de fixture (quais usuários o oráculo referencia,
candidatos/pertences item→contexto/pré-materialização desses usuários) foi
extraída para [`harness/fixtures.py`](harness/fixtures.py), compartilhada
por todo carregador `schemas/<db>/load_oracle_fixture.py` — cada banco novo
só escreve a parte de "como gravar isso nesse banco", não como ler os
parquets de novo.

#### Etapa 5 — Valkey (BD-2)

`storage/valkey.py` implementa as 4 primitivas com estruturas
nativas do Valkey — HASH para candidatos, SET para pertença item→contexto e
para listas invertidas (globais, não truncadas), e pré-materialização como
HASH por (usuário, contexto). **Decisão de implementação**: a imagem hoje
usada (`valkey/valkey:8-alpine`) não tem um módulo de busca
(RediSearch-equivalente), que era a técnica que `CONTEXTO.md` original
cogitava para E-2; em vez disso, E-2 usa um script Lua que avalia o
predicado dentro do Valkey (nunca busca tudo para o cliente filtrar), e E-4
usa `SINTERSTORE` — o predicado continua avaliado no banco, só não com a
técnica literal do documento original. Ver a docstring de
`storage/valkey.py` e o comentário do serviço `valkey` em
`docker-compose.yml`.

`VIABLE_CELLS` agora também tem `e1-valkey`, `e2-valkey`, `e3-valkey`,
`e4-valkey` — as 4 estratégias são viáveis em Valkey por `CONTEXTO.md`, e
as 4 passam pelos 1000 casos do oráculo.

Como rodar:

```
docker compose build tools
docker compose run --rm tools -m "not integration" -v   # camada rápida

# camada de integração — exige Postgres + Valkey reais + massa gerada
docker compose up -d postgres valkey
docker compose run --rm --entrypoint python tools schemas/postgres/load_oracle_fixture.py
docker compose run --rm --entrypoint python tools schemas/valkey/load_oracle_fixture.py
docker compose run --rm tools -m integration tests/acceptance/test_harness_all_cells.py -v
docker compose stop postgres valkey
```

> Mesmo cuidado de item_id fora de faixa se aplica a testes de integração
> contra Valkey: `inverted:{context_id}` é uma chave GLOBAL compartilhada
> com dado real; teardown usa `SREM` (remove só os membros de teste), nunca
> `DEL` na chave inteira — ver `storage/tests/test_valkey_adapter.py`.

#### Etapa 5 — ScyllaDB (BD-3), incluindo a primeira célula inviável

Terceira tecnologia da Etapa 5. Por
`CONTEXTO.md`, E-4 é **inviável** em Scylla (sem primitiva de interseção de
conjuntos em CQL) — `storage/scylla.py` não implementa `intersect`, e
`supported_primitives` não inclui essa primitiva; chamar mesmo assim
levanta `PrimitiveNotSupported` (comportamento herdado da classe base).

**Modelagem CQL** (sem joins, sem filtro arbitrário fora da chave de
partição/clustering): `candidates(user_id, rank)` para leitura direta de
todos os candidatos; `item_contexts(item_id, context_id)` para a pertença
item→contexto; e uma tabela desnormalizada,
`candidates_by_context((context_id, user_id), rank)`, particionada por
(contexto, usuário) — leitura direta e já filtrada por um único contexto
(E-2). Como essa leitura nunca é truncada, contexto composto (E-2 com dois
`context_id`) lê uma partição por contexto e intersecta em Python
corretamente — ao contrário do top-40 de E-3 sobre `prematerialized`, que
tem a mesma limitação de contexto único já registrada na Etapa 4.

**`tests/acceptance/test_infeasible_cells_fail_at_startup.py`** — primeiro
teste real de célula inviável: `check_compatibility(E4Intersection(),
ScyllaAdapter)` recebe a **classe**, não uma instância, então a checagem
não abre conexão nenhuma — roda na camada rápida, sem Scylla no ar, e ainda
assim comprova a falha na montagem, citando `intersect` e `scylla` na
mensagem.

**Três bugs reais encontrados e corrigidos ao validar esta etapa** (fica
registrado porque são armadilhas prováveis de reaparecer em OpenSearch):

1. `session.execute(query, (item_ids,))` com uma lista/tupla Python para
   `IN %s` dá erro de sintaxe no parser do Scylla — é preciso um
   placeholder `%s` por valor (`IN (%s, %s, ...)`, construído no tamanho
   do lote), não um único `%s` para a lista inteira.
2. Scylla limita `IN` de chave de partição a 100 valores por consulta por
   padrão — um usuário pode ter até N=500 candidatos (`CONTEXTO.md`), então
   a busca em lote de `item_contexts` precisa ser feita em pedaços de ≤100.
3. Inserir ~850 mil linhas uma a uma (mesmo concorrente) no
   `candidates_by_context` saturava o único shard do cluster de
   desenvolvimento (`--smp 1`), com `WriteTimeout` do próprio coordenador —
   não um problema de timeout do cliente. A correção foi agrupar por
   partição `(context_id, user_id)` e escrever cada uma como um `BATCH
   UNLOGGED`, caindo para poucos milhares de requisições.

Como rodar:

```
docker compose build tools
docker compose run --rm tools -m "not integration" -v   # camada rápida (inclui o teste de célula inviável)

# camada de integração — exige Postgres + Valkey + Scylla reais + massa gerada
docker compose up -d postgres valkey scylla
docker compose run --rm --entrypoint python tools schemas/scylla/apply_schema.py
docker compose run --rm --entrypoint python tools schemas/postgres/load_oracle_fixture.py
docker compose run --rm --entrypoint python tools schemas/valkey/load_oracle_fixture.py
docker compose run --rm --entrypoint python tools schemas/scylla/load_oracle_fixture.py
docker compose run --rm tools -m integration tests/acceptance/test_harness_all_cells.py -v
docker compose stop postgres valkey scylla
```

#### Etapa 5 — OpenSearch (BD-4), incluindo a segunda célula inviável — fecha a matriz de corretude

Quarta e última tecnologia da Etapa 5. Por `CONTEXTO.md`, E-3
(pré-materialização) **não tem sentido arquitetural** aqui — pré-
materializar respostas chave-valor não aproveita nada de um índice
invertido. `storage/opensearch.py` não implementa `get_prematerialized`, e
`supported_primitives` não inclui essa primitiva.

**Modelagem**: um documento por (usuário, item) no índice `candidates`,
com `context_ids` como campo numérico multi-valor. `get_candidates_filtered`
usa uma bool query com uma cláusula `term` por `context_id` pedido —
interseção AND resolvida nativamente pelo índice invertido do Lucene, sem
workaround (E-2 "nativo" por `CONTEXTO.md`). Diferente de Valkey/Scylla,
cada documento pertence só a um usuário — não há estrutura global
compartilhada entre usuários, então um `user_id` fora da faixa real já
isola os testes de integração, sem o risco de colisão que apareceu nas
etapas anteriores.

`intersect` usa a mesma técnica de consulta de `get_candidates_filtered`
por ora — a distinção real de E-2 vs. E-4 é uma otimização de latência da
Fase 2, fora do escopo desta etapa (corretude), mesma decisão já tomada
para Postgres/Scylla/Valkey.

Com isso, `tests/acceptance/test_infeasible_cells_fail_at_startup.py` tem
as 2 células inviáveis da matriz completas (E-4/Scylla, E-3/OpenSearch), e
`VIABLE_CELLS` em `tests/acceptance/test_harness_all_cells.py` tem as 14
células viáveis das 16 da matriz 4×4 — **todas passam pelos 1000 casos do
oráculo**. Isso fecha a verificação de corretude da Fase 1 do TCC: resta
`cells/` (config YAML + validação) para tornar a seleção de célula
dirigida por configuração em vez de hardcoded no teste, o que não muda
nenhum resultado de corretude já obtido, só a forma de selecionar a célula.

Como rodar:

```
docker compose build tools
docker compose run --rm tools -m "not integration" -v   # camada rápida (as 2 células inviáveis)

# camada de integração — exige as 4 bancos reais + massa gerada
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

#### Etapa 5 — `cells/` (configuração de célula) — fecha a Etapa 5

Último pedaço da Etapa 5. `core/config.py` carrega e valida a configuração
de uma célula: `cells/_defaults.yaml` guarda o que é comum (hoje: `cache`,
`transport`, `params` — `n_candidates`, `k`, `prematerialized_m`,
`exclusion_size`, os parâmetros fixos de `CONTEXTO.md`); cada
`cells/<id>.yaml` sobrepõe só `id`, `strategy`, `storage` e
`storage_config`. Validado com Pydantic e `extra="forbid"` — um campo
desconhecido (erro de digitação) levanta erro em vez de virar configuração
padrão silenciosamente, tanto no nível raiz quanto dentro de `params`.

As 14 células viáveis têm um arquivo `cells/<id>.yaml` cada, com o mesmo
`id` usado em `VIABLE_CELLS` (`tests/acceptance/test_harness_all_cells.py`)
e nos nomes dos bancos do `docker-compose.yml` como `host`. Isso só valida
e carrega a configuração — a montagem real da célula (mapear `strategy`
para uma classe de `strategies/` e `storage` para uma classe de
`storage/`) é responsabilidade de `service/main.py`, que ainda não existe;
por isso `VIABLE_CELLS` continua hardcoded no teste por enquanto, e não
lendo `cells/*.yaml` diretamente.

Como rodar:

```
docker compose build tools
docker compose run --rm tools -m "not integration" -v   # camada rápida, sem banco
```

---

Com isso, a **Etapa 5 está completa** e a verificação de corretude da Fase
1 do TCC (matriz 4×4 de estratégias × bancos) está fechada: das 16
combinações, 14 são viáveis e todas passam pelos 1000 casos do oráculo: as
2 inviáveis (E-4/Scylla, E-3/OpenSearch) falham corretamente na montagem da
célula, com mensagem clara, sem precisar de conexão. Nenhuma latência foi
medida em nenhum momento deste trabalho — por design (ver `CONTEXTO.md`,
"regra de ouro da implementação"): essa é a Fase 1 de medição real,
Terraform e nuvem, ainda não iniciada.

### Fase 3 — service/, load/, analysis/, infra/ (rumo à medição real)

#### Etapa 6 — `service/`

Primeiro lugar onde `strategy`+`storage` viram um processo de verdade
respondendo por HTTP/gRPC — sem isso, `load/` não teria o que testar.
`core/registry.py` é a fonte única de verdade que resolve as strings
`strategy`/`storage` de `cells/*.yaml` para as classes reais — substitui o
`VIABLE_CELLS` que vivia hardcoded em `test_harness_all_cells.py` (o
próprio teste avisava sobre isso desde a Etapa 5). `build_storage(config)`
monta cada adaptador a partir só de `host`/`port` da config; credenciais
(Postgres precisa de usuário/senha/banco) vêm exclusivamente do ambiente —
nunca do YAML versionado.

`service/http_app.py` usa **Hypercorn**, não uvicorn (uvicorn não
implementa HTTP/2) — documentado em `CONTEXTO.md`, seção "Pilha", com a
mesma disciplina já aplicada à troca Locust→k6. T-A (HTTP/1.1) e T-B
(HTTP/2 cleartext) são o **mesmo processo**: rodando sem TLS, o Hypercorn
aceita ambos automaticamente — não existe uma flag "ligar HTTP/2" a
configurar; a distinção vive inteiramente do lado do cliente de carga.
**Confirmado com um handshake HTTP/2 "prior knowledge" manual** (via a
biblioteca `h2` direto, sem passar por nenhum cliente HTTP de alto nível)
contra o serviço rodando: a resposta veio com `server: hypercorn-h2`,
provando que o HTTP/2 de verdade funciona, não só HTTP/1.1 disfarçado.

`service/grpc_app.py` + `service/proto/recommendation.proto` (T-C) estão
implementados e testados, mas **não** entram em `load/` ainda — Fase 2
(comparação de transporte) só faz sentido depois que a Fase 1 escolher a
célula vencedora via medição real na nuvem, o que ainda não aconteceu.

Uma amostra de 20 casos reais do oráculo passa pelo `service` real via HTTP
de verdade em `tests/acceptance/test_service_smoke.py` — a única camada que
prova que a serialização de rede não corrompe nada (o `harness/` nunca passa
por HTTP, testa `strategy.retrieve` direto).

Como rodar:

```
docker compose build tools service
docker compose run --rm tools -m "not integration" -v   # camada rápida

# camada de integração — exige Postgres real + massa gerada + o service no ar
docker compose up -d postgres
docker compose run --rm --entrypoint python tools schemas/postgres/load_oracle_fixture.py
docker compose up -d service
docker compose run --rm tools -m integration tests/acceptance/test_service_smoke.py -v
docker compose run --rm tools -m integration tests/acceptance/test_harness_all_cells.py -v  # só as 4 células postgres passam sem valkey/scylla/opensearch no ar
docker compose stop postgres service
```

> `ScyllaAdapter.__init__` resolve/conecta no construtor (diferente de
> Postgres/Valkey/OpenSearch, que são preguiçosos) — decisão já tomada na
> Etapa 5. Por isso `service/tests/test_registry.py::test_build_storage_scylla_needs_no_credential`
> é `integration`, ao contrário dos testes equivalentes dos outros 3 bancos.

#### Etapa 7 — `load/`

Gerador de carga contra o `service`. **k6**, não Locust — decisão finalizada em
`CONTEXTO.md`, "Pilha": Locust é de malha fechada (omissão coordenada quando o
serviço degrada), k6 tem executor de taxa de chegada constante (modelo aberto
de verdade) e suporte nativo a gRPC.

`load/scenarios.js` usa o executor **`constant-arrival-rate`** — confirmado
via `k6 inspect` — com dois cenários por execução: `warmup` (2 min,
descartado) seguido de `measurement` (`startTime: '2m'`, 5 min), usando a tag
automática `scenario` do k6 (não timestamps) para a Etapa 8 filtrar o que
entra na análise. Um segundo modo (`RAMP_MODE=true`) troca para
`ramping-arrival-rate` com `abortOnFail` nos thresholds — implementa a
"rampa até violar o SLO" (p99 > 200 ms ou taxa de erro > 1%, CONTEXTO.md)
parando sozinho quando o SLO estoura, em vez de rodar a rampa inteira.

`load/zipf.js` amostra `user_id` por Zipf (expoente 1,0, fixo pelo desenho
experimental) — sem lib externa no runtime do k6, a CDF é pré-computada uma
única vez via `SharedArray` (não recomputada por VU) e amostrada por busca
binária.

`load/export_contexts_by_tier.py` reaproveita `candidate_selectivity`/`tier`
de `data_generation/data/contexts.parquet` (calculados na Etapa 3, nunca
recalculados aqui) para gerar `load/fixtures/contexts_by_tier.json`, que o
k6 carrega via `SharedArray`/`open()` para escolher o único `context_id` do
patamar de seletividade pedido (`SELECTIVITY_TIER=high|medium|low`, ~2%/20%/60%
dos candidatos sobrevivendo — os nomes dos patamares seguem a convenção já
fixada em `data_generation/generator/config.py:TIER_TARGETS`: "high" é
seletividade *alta* = predicado mais restritivo = menos itens sobrevivem,
não o contrário).

`load/run_battery.py` embaralha a lista de células viáveis (seed logada) e
roda `load/scenarios.js` N vezes por célula, salvando
`results/<cell-id>/<phase>/<timestamp>/rep<N>/{manifest.json,k6-raw.json}` —
`cells/*.yaml` (exceto `_defaults.yaml`) já É a lista de 14 células viáveis,
sem duplicar a checagem em nenhum outro lugar.

**Limitação conhecida, documentada em `load/scenarios.js`:** o k6 negocia
HTTP/2 só sobre TLS (ALPN) — não fala h2c ("prior knowledge") em texto puro.
`service/http_app.py` já expõe h2c real (verificado manualmente na Etapa 6),
mas o k6 só vai conseguir exercitá-lo com TLS na frente do serviço — decisão
adiada para quando a Fase 2 (comparação de transporte) começar de fato.
T-C (gRPC) continua fora de `load/` pelo mesmo motivo já registrado na
Etapa 6.

Como rodar (SMOKE local — CONTEXTO.md proíbe medir latência aqui):

```
docker compose build tools
docker compose up -d postgres
docker compose run --rm --entrypoint python tools schemas/postgres/load_oracle_fixture.py
docker compose up -d service

docker compose run --rm --entrypoint python tools load/export_contexts_by_tier.py
docker compose run --rm --entrypoint k6 tools inspect load/scenarios.js -e CELL=e1-postgres   # confirma o executor
docker compose run --rm --entrypoint k6 tools run load/scenarios.js --vus 1 --duration 10s \
    -e CELL=e1-postgres -e TARGET_URL=http://service:8000/v1/recommendations

docker compose run --rm --entrypoint python tools load/run_battery.py \
    --cells e1-postgres --target-url http://service:8000/v1/recommendations \
    --repetitions 1 --smoke

docker compose stop postgres service
```

#### Etapa 8 — `analysis/`

Consolida a saída bruta do k6 no formato de `results/` (IMPLEMENTACAO.md,
"Coleta de resultados") e fornece a estatística exigida por CONTEXTO.md.

`analysis/collect.py` lê `k6-raw.json` (NDJSON do `--out json=` do k6) e
reconstrói `latencies.parquet` — uma linha por requisição, com timestamp,
latência, status e `returned_count`. `returned_count` é uma métrica
customizada (`Trend`) adicionada a `load/scenarios.js` nesta etapa,
correlacionada com `http_req_duration` pelo tag `request_id` (o k6 não
junta métricas sozinho). `summary.json` é calculado em Python a partir do
mesmo `latencies.parquet` — não a partir do `handleSummary()` do k6 — para
que percentis e o bootstrap de `analysis/stats.py` usem exatamente o mesmo
método de quantil, sem duas implementações (uma em JS, outra em Python)
que pudessem divergir. Só conta como `measurement`/`ramp_to_slo` (nunca
`warmup`, nunca `default` — este último é a tag que o k6 usa quando
`--vus`/`--duration` sobrescrevem os cenários, como nos smokes locais; um
`collect.py` rodado sobre saída de smoke corretamente devolve 0 requisições
e um `latencies.parquet` vazio, mas com o schema certo — smoke não é
medição, CONTEXTO.md proíbe medir aqui).

`analysis/stats.py` implementa exatamente o que CONTEXTO.md, "Estatística",
pede: `kruskal_wallis`, `dunn_posthoc` (Bonferroni), `bootstrap_percentile_ci`
(10.000 reamostras), `effect_size_epsilon_squared` (companion não-paramétrico
do Kruskal-Wallis — Cohen's d não se aplica porque assume normalidade) e
`tost_equivalence` (dois testes t de uma cauda, para a etapa de confirmação
alegar equivalência prática entre células da fronteira de Pareto, não só
"não rejeitou H0").

`analysis/resources.py` (CPU/memória) e `analysis/storage_size.py` (volume
em disco) seguem o mesmo padrão de `storage/base.py` — uma interface,
vários backends. `DockerStatsCollector` (local) está implementado e
testado (parsing de `docker stats`); `GCPMonitoringCollector` levanta
`NotImplementedError` de propósito, porque depende de `infra/` (Etapa 9,
ainda não implementada). Em `storage_size.py`, só Postgres tem consulta
real (`pg_database_size`); os outros três bancos ficam para quando houver
uma instância de nuvem de verdade para validar o formato de saída de cada
ferramenta.

`analysis/plots.py` gera os 4 gráficos do texto do TCC: fronteira de
Pareto (latência p99 × custo, triagem), comparação de percentis entre
células da fronteira (confirmação), latência × vazão (rampa até o SLO) e
taxa de acerto de cache (H3, hoje sempre 0 — nenhuma célula tem cache
ainda).

Como rodar:

```
docker compose run --rm tools -m "not integration" analysis/tests -v

# sobre a saída de um smoke test da Etapa 7 (confirma o formato dos
# arquivos, não produz números que signifiquem nada — CONTEXTO.md proíbe
# medir localmente):
docker compose run --rm --entrypoint python tools analysis/collect.py \
    results/e1-postgres/triagem/<timestamp>/rep0
```

#### Etapa 9 — `infra/` (Terraform, GCP) — template, sem projeto GCP real

Esqueleto completo dos 4 módulos + 3 aplicações Terraform pedidos por
IMPLEMENTACAO.md, construído e validado (`fmt`/`validate`) sem nenhuma
credencial real — só falta `project_id`/conta de faturamento de verdade
(que o usuário ainda não tem) e a autenticação do bootstrap para virar
infraestrutura de fato. **Nenhum `plan`/`apply` real foi rodado nesta
etapa** — são recursos faturáveis, e IMPLEMENTACAO.md exige confirmação
explícita e separada antes de qualquer um.

`infra/bootstrap/` — única exceção documentada a "nunca estado local"
(o bucket de estado remoto precisa existir antes de qualquer outro
`terraform init -backend-config` poder apontar para ele). Cria o bucket de
estado (sem `prevent_destroy`) e o bucket de resultados (com
`prevent_destroy` — a única exceção do projeto, IMPLEMENTACAO.md, "Custo").

`infra/modules/` — `network` (VPC + sub-rede privada, firewall SSH só via
faixa do IAP `35.235.240.0/20`, tráfego interno liberado só dentro da
sub-rede, Cloud NAT para saída sem IP público); `database` (Compute Engine
rodando a MESMA imagem/flags Docker de `docker-compose.yml` — nunca um
banco gerenciado; `storage` seleciona imagem/flags, `cell` só rotula,
evitando duplicar em HCL o parsing que `cells/*.yaml` já faz); `service`
(startup script busca a senha do Postgres no Secret Manager antes do
`docker run` — decisão já registrada na Etapa 6/7: infra/, não Python, é
responsável por isso); `loadgen` (VM obrigatoriamente separada da de
serviço, mesma imagem `tools` do Etapa 7 — a metodologia exige comprovar
que a CPU do gerador ficou abaixo de 60%).

`infra/envs/experiment/` — wire dos 4 módulos; variáveis `project_id`,
`cell`, `storage` etc. com `validation` (`cell` só aceita uma das 14
células viáveis; `storage` só os 4 bancos reais); backend GCS com
`bucket`/`prefix` vindos de `-backend-config` na linha de comando, nunca
hardcoded — isso é o que isola o estado por célula (IMPLEMENTACAO.md,
"menor risco de `terraform workspace select` errado destruir a célula
errada"). `terraform.tfvars.example` versionado com placeholders;
`terraform.tfvars` de verdade nunca é commitado.

`infra/envs/budget/` — `google_billing_budget` como aplicação separada,
rodada uma vez antes de qualquer célula (não é uma dependência de grafo do
Terraform, é ordem procedural).

`infra/scripts/snapshot_after_load.sh` — snapshot do disco de dados via
`gcloud`, chamado de fora do Terraform (nunca `local-exec`: carga de dados
é lógica de aplicação).

Como rodar (só `fmt`/`validate` — nenhum plan/apply real):

```
docker compose build tools   # inclui o binário do Terraform (hashicorp/terraform:1.9.8)

docker compose run --rm --entrypoint terraform tools fmt -check -recursive -diff infra/

for dir in bootstrap modules/network modules/database modules/service modules/loadgen envs/experiment envs/budget; do
  docker compose run --rm --entrypoint terraform tools -chdir=infra/$dir init -backend=false
  docker compose run --rm --entrypoint terraform tools -chdir=infra/$dir validate
done
```

> `terraform init` (mesmo com `-backend=false`) gera `.terraform.lock.hcl`
> em cada diretório — versionado de propósito (IMPLEMENTACAO.md), ao
> contrário de `.terraform/` (cache de provedores) e `*.tfstate*`, ambos
> no `.gitignore`. `infra/` é montado como bind mount read-write em
> `docker-compose.yml` (não `COPY`, ao contrário do resto do repositório)
> exatamente para que esses arquivos apareçam no host.

### Fase 4 — Deploy em nuvem (GCP)

Passo a passo real, com credenciais e recursos faturáveis — cada `apply`
abaixo custa dinheiro e exige confirmação explícita antes de rodar.

**Compute Engine, não Cloud Run.** A camada de serviço roda em VM
(`infra/modules/service/`), não em execução serverless — está fora de
escopo do projeto (`CLAUDE.md`: "serverless execution"), e é consistente
com `infra/modules/database/` já rodando a mesma imagem Docker do
`docker-compose.yml` local em vez de um banco gerenciado. Autoscaling e
cold starts de uma plataforma serverless contaminariam justamente a cauda
de latência (p95/p99/p99.9) que o estudo mede.

**0. Pré-requisitos manuais (fora deste repositório)**
- Criar um projeto GCP e vincular uma conta de faturamento.
- Habilitar as APIs: Compute Engine, Cloud Storage, Secret Manager,
  Artifact Registry, Cloud Billing Budget, IAM, Identity-Aware Proxy.
- `gcloud auth login` na máquina host (fora de qualquer container — os
  scripts abaixo usam o `gcloud` do host, não o do container `tools`, que
  só tem `terraform` e `k6`).

**1. Credencial do Terraform — nunca dentro do repositório**

O Terraform roda dentro do container `tools`, que precisa de uma
credencial GCP. Em vez de montar as credenciais pessoais do usuário
(`~/.config/gcloud`, que dá dor de cabeça no Docker Desktop/Windows e
mistura identidade pessoal com automação), uma service account dedicada é
criada e sua chave é gerada **fora da árvore do Git** — nunca dentro de
`infra/` ou de qualquer pasta versionada, mesmo com `.gitignore`:

```
infra/scripts/create_terraform_service_account.sh <project-id> <billing-account-id> "$HOME/.tcc-secrets/terraform-key.json"
```

O script se recusa a escrever a chave se o caminho de saída cair dentro do
repositório — checagem ativa (`git rev-parse --show-toplevel`), não só
documentação. Ele concede os papéis mínimos que os recursos reais de
`infra/` exigem: `roles/compute.admin` (VMs e rede), `roles/storage.admin`
(buckets do bootstrap), `roles/iam.serviceAccountAdmin` +
`roles/iam.serviceAccountUser` (a SA de runtime que
`infra/modules/service/` cria para a própria VM de serviço),
`roles/resourcemanager.projectIamAdmin` (a concessão de
`secretmanager.secretAccessor` a essa SA), e `roles/billing.admin` na
conta de faturamento (`google_billing_budget` exige isso separadamente,
fora do projeto).

Depois de gerada, exporte o caminho no shell do host (nunca num `.env`
versionado) antes de qualquer comando Terraform via `docker-compose.gcp.yml`:

```
export GCP_TERRAFORM_KEY_PATH="$HOME/.tcc-secrets/terraform-key.json"
docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
  run --rm --entrypoint terraform tools -chdir=infra/bootstrap apply -var="project_id=<seu-projeto>"
```

`docker-compose.gcp.yml` é um overlay opcional — nunca usado sozinho,
sempre com `-f docker-compose.yml -f docker-compose.gcp.yml`. Ele monta a
chave como um único arquivo read-only, sem copiá-la para dentro da árvore
do Git.

**2. Bootstrap — buckets de estado e resultados**

```
docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
  run --rm --entrypoint terraform tools -chdir=infra/bootstrap init
docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
  run --rm --entrypoint terraform tools -chdir=infra/bootstrap apply -var="project_id=<seu-projeto>"
```

Guarde os outputs `terraform_state_bucket` e `results_bucket` — usados nos
passos seguintes.

**3. Orçamento — antes de qualquer VM**

```
docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
  run --rm --entrypoint terraform tools -chdir=infra/envs/budget \
  init -backend-config="bucket=<terraform_state_bucket>"
docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
  run --rm --entrypoint terraform tools -chdir=infra/envs/budget apply \
  -var="project_id=<seu-projeto>" -var="billing_account_id=<sua-conta>" -var="monthly_budget_usd=<valor>"
```

**4. Imagens — build e push para o Artifact Registry**

Deliberadamente fora do Terraform (`infra/modules/service/main.tf`:
"publicada em Artifact Registry fora deste [terraform]"). Roda no host,
com a sessão pessoal do usuário (`gcloud auth configure-docker`), não a
service account do Terraform:

```
infra/scripts/build_and_push_images.sh <project-id> <region> [tag]
```

Cria o repositório Artifact Registry `tcc` se ainda não existir, builda
`docker/Dockerfile.service` e `docker/Dockerfile.tools`, e faz push das
duas — imprimindo as referências completas para colar em
`service_image`/`tools_image` do `terraform.tfvars` (passo 6).

**5. Secret Manager — senha do Postgres**

O startup script de `infra/modules/service/` busca a senha em
`gcloud secrets versions access latest --secret=tcc-postgres-password` —
não existe recurso Terraform para criar o secret (checado em
`infra/modules/service/main.tf`), é um passo manual:

```
echo -n "<senha-real>" | gcloud secrets create tcc-postgres-password --data-file=- --project=<seu-projeto>
```

**6. Por célula — plan/apply**

```
cd infra/envs/experiment
cp terraform.tfvars.example terraform.tfvars   # preencher project_id, cell, storage, service_image, tools_image; nunca commitar
```

```
docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
  run --rm --entrypoint terraform tools -chdir=infra/envs/experiment \
  init -backend-config="bucket=<terraform_state_bucket>" -backend-config="prefix=cells/<cell>"
docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
  run --rm --entrypoint terraform tools -chdir=infra/envs/experiment plan
docker compose -f docker-compose.yml -f docker-compose.gcp.yml \
  run --rm --entrypoint terraform tools -chdir=infra/envs/experiment apply   # faturável — confirmar antes
```

Depois de medir, `terraform destroy` a célula (o disco/snapshot sobrevive
fora do ciclo de vida da VM — `infra/scripts/snapshot_after_load.sh`) antes
de aplicar a próxima, para não pagar por VMs ociosas.

### Smoke test em nuvem — antes de qualquer bateria real

`infra/scripts/cloud_smoke_test.py` sobe uma célula de verdade, aplica
schema, carrega o subconjunto pequeno de dados do oráculo
(`harness/fixtures.py`), roda `harness/verify_cli.py` como gate de
corretude, dispara uma carga leve (`SMOKE_MODE=true` em
`load/scenarios.js`), imprime uma sanidade grosseira de latência/recursos
(`docker stats` via SSH, não `analysis/resources.py` — esse continua só
local), e derruba tudo no final — pensado para pegar problemas antes da
bateria de medição real (cara, demorada, não deveria precisar ser
re-executada). Roda no host (usa o `gcloud` do host para SSH via IAP nas
VMs, todas sem IP público):

```
export TOOLS_IMAGE=us-central1-docker.pkg.dev/<seu-projeto>/tcc/tools:latest
python infra/scripts/cloud_smoke_test.py e1-postgres <project-id> us-central1 us-central1-a <terraform_state_bucket>
```

Cada `terraform apply`/`destroy` dentro dele é anunciado explicitamente e
pede confirmação — mesmo já tendo sido descrito aqui. `--keep-infra` pula o
destroy final, para investigar uma falha manualmente.
