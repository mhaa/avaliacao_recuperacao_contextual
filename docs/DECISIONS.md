# Histórico de decisões e bugs encontrados

Narrativa técnica por etapa: decisões de implementação, bugs reais encontrados
e corrigidos, e as pegadinhas que valeram a pena documentar para não
reaparecerem. Para o passo a passo de reprodução, veja o [README](../README.md);
para o "porquê" arquitetural atemporal, veja [ARCHITECTURE.md](ARCHITECTURE.md)
e [DESIGN.md](DESIGN.md).

## Etapa 3 — harness/ (oráculo + verificação) <a id="etapa-3"></a>

Como o teste de ponta a ponta precisa de dado real equivalente ao que gerou o
oráculo, `schemas/postgres/load_oracle_fixture.py` carrega em uma Postgres já
com o esquema aplicado só os candidatos e pertences item→contexto dos usuários
referenciados pelos 1000 casos (não a base inteira). A lógica de carregamento
de fixture foi extraída para `harness/fixtures.py`, compartilhada por todo
carregador `schemas/<db>/load_oracle_fixture.py` — cada banco novo só escreve
a parte de "como gravar isso nesse banco", não como ler os parquets de novo.

**Bug real encontrado e corrigido**: nunca hardcode `item_id`s "de teste"
dentro da faixa real do catálogo (denso, `0..87584`) — um teardown que faz
`DELETE ... WHERE item_id = ANY(...)` apagaria dado real carregado por
`load_oracle_fixture.py` sem erro visível, só corrompendo silenciosamente
outro teste rodado depois na mesma sessão de banco. Encontrado ao validar esta
etapa — ver `storage/tests/test_postgres_adapter.py`.

## Etapa 4 — E-2, E-3, E-4 sobre Postgres <a id="etapa-4"></a>

`strategies/e3_prematerialized.py` tem uma decisão de implementação que vale
registrar: `prematerialized.parquet` guarda só o top-40 por (usuário, contexto
ÚNICO) — intersectar dois top-40 (um por contexto) não garante o mesmo
resultado do oráculo, que filtra sobre os 500 candidatos completos. Por isso
E-3 só usa a leitura pré-materializada quando a requisição pede um único
contexto; para contexto composto, cai para leitura completa + filtro em
aplicação (mesmo caminho de E-1). Isso é um resultado citável do trabalho
(E-3 não tem vantagem em contexto composto), não um bug — ver a docstring do
módulo para os detalhes.

**E-4/Postgres — de placeholder a técnica real (revisão pós-Etapa 5).** A
primeira versão de `intersect` reaproveitava a mesma SQL de
`get_candidates_filtered` (E-2) só com `LIMIT` — suficiente para o harness de
corretude, mas identificado em revisão como bloqueador antes da triagem: E-2
e E-4 mediriam o mesmo plano de execução em Postgres, tornando a comparação
entre as duas estratégias vazia por construção nessa tecnologia. Corrigido
usando `intarray` (contrib oficial do Postgres) sobre uma nova tabela
`inverted_lists(context_id, item_ids[])` — lista invertida global por
contexto, carregada de `data_generation/data/inverted_lists.parquet` via
`harness/fixtures.py:load_inverted_lists()` (existia desde a geração de
dados, mas nenhum loader do Postgres a consumia). O operador `&` intersecta
o array de candidatos do usuário com a lista invertida do(s) contexto(s)
pedido(s) — mecanismo de array-merge sobre dado desnormalizado, distinto do
JOIN+GROUP BY/HAVING de E-2 sobre `item_contexts` (normalizado).

Prioridade deliberada por uma extensão **nativa** do Postgres: `intarray` é
contrib oficial, mantido junto do core e já compilado na imagem oficial —
não uma adição externa que precisa ser buscada, compilada e mantida à parte.
Considerado e descartado `pg_roaringbitmap` por ser exatamente o oposto
disso — uma extensão externa **não oficial** (repositório de terceiro, fora
do projeto Postgres) — apesar de o pipeline já gerar `inverted_bitmaps/*.bin`
em formato Roaring via `pyroaring`, especificamente para esse tipo de
consumo: não é módulo contrib do Postgres, exigiria compilar e publicar uma
imagem Postgres customizada — tanto localmente
(`docker-compose.yml`, hoje `postgres:16-alpine` oficial) quanto na nuvem
(`infra/modules/database/main.tf`, mesma imagem oficial puxada via
`docker pull` numa VM Container-Optimized OS, que não tem toolchain de
build). Seria uma mudança de infraestrutura nova e permanente, fora do
escopo de corrigir `intersect` — e reabriria a classe de risco de imagem de
serviço dessincronizada já vivida neste projeto. `intarray` não exige nada
disso: já vem compilado na imagem oficial, só precisa de
`CREATE EXTENSION`.

Vale registrar o "nível" de lista invertida aqui: `inverted_lists` é
estruturalmente uma lista invertida (contexto→itens materializado), e `#`
é a primitiva nativa de intersecção de conjuntos do Postgres sobre esse
dado — mas, ao contrário do índice invertido do Lucene (OpenSearch), a
intersecção não é feita PERCORRENDO um índice: os dois arrays são buscados
por chave e intersectados fora de qualquer estrutura de índice secundária.
É o mesmo nível do Valkey (`SINTER` sobre dois SETs buscados por chave) —
abaixo do nível do OpenSearch (intersecção index-resident via skip-list),
acima do nível de um JOIN linha-a-linha sobre dado normalizado (E-2). Sem
índice GIN em `inverted_lists`: o acesso é sempre direto por `context_id`
(chave primária); GIN só aceleraria "quais linhas contêm o item X", que não
é o padrão de acesso de `intersect`.

## Etapa 5 — Valkey (BD-2) <a id="etapa-5-valkey"></a>

**Decisão de implementação**: a imagem usada (`valkey/valkey:8-alpine`) não
tem um módulo de busca (RediSearch-equivalente), que era a técnica que o
desenho original cogitava para E-2; em vez disso, E-2 usa um script Lua que
avalia o predicado dentro do Valkey (nunca busca tudo para o cliente
filtrar), e E-4 usa `SINTERSTORE` — o predicado continua avaliado no banco,
só não com a técnica literal do documento original. Ver a docstring de
`storage/valkey.py` e o comentário do serviço `valkey` em
`docker-compose.yml`.

**Cuidado equivalente ao da Etapa 3** se aplica a testes de integração contra
Valkey: `inverted:{context_id}` é uma chave GLOBAL compartilhada com dado
real; teardown usa `SREM` (remove só os membros de teste), nunca `DEL` na
chave inteira — ver `storage/tests/test_valkey_adapter.py`.

## Etapa 5 — ScyllaDB (BD-3), incluindo a primeira célula inviável <a id="etapa-5-scylla"></a>

Por [DESIGN.md](DESIGN.md), E-4 é **inviável** em Scylla (sem primitiva de
interseção de conjuntos em CQL) — `storage/scylla.py` não implementa
`intersect`, e `supported_primitives` não inclui essa primitiva; chamar mesmo
assim levanta `PrimitiveNotSupported` (comportamento herdado da classe base).

**Modelagem CQL** (sem joins, sem filtro arbitrário fora da chave de
partição/clustering): `candidates(user_id, rank)` para leitura direta de
todos os candidatos; `item_contexts(item_id, context_id)` para a pertença
item→contexto (varrida por inteiro uma vez na montagem, não mais uma consulta
por item, por requisição); e uma tabela desnormalizada,
`candidates_by_context((context_id, user_id), rank)`, particionada por
(contexto, usuário) — leitura direta e já filtrada por um único contexto
(E-2). Como essa leitura nunca é truncada, contexto composto (E-2 com dois
`context_id`) lê uma partição por contexto e intersecta em Python
corretamente — ao contrário do top-40 de E-3 sobre `prematerialized`, que tem
a mesma limitação de contexto único já registrada na Etapa 4.

`tests/acceptance/test_infeasible_cells_fail_at_startup.py` é o primeiro teste
real de célula inviável: `check_compatibility(E4Intersection(), ScyllaAdapter)`
recebe a **classe**, não uma instância, então a checagem não abre conexão
nenhuma — roda na camada rápida, sem Scylla no ar, e ainda assim comprova a
falha na montagem, citando `intersect` e `scylla` na mensagem.

**Três bugs reais encontrados e corrigidos ao validar esta etapa** (fica
registrado porque são armadilhas prováveis de reaparecer em qualquer banco
com modelo de particionamento parecido):

1. `session.execute(query, (item_ids,))` com uma lista/tupla Python para
   `IN %s` dá erro de sintaxe no parser do Scylla — é preciso um placeholder
   `%s` por valor (`IN (%s, %s, ...)`, construído no tamanho do lote), não um
   único `%s` para a lista inteira.
2. Scylla limita `IN` de chave de partição a 100 valores por consulta por
   padrão — um usuário pode ter até N=500 candidatos, então qualquer busca em
   lote por chave de partição precisa ser feita em pedaços de ≤100.
3. Inserir ~850 mil linhas uma a uma (mesmo concorrente) no
   `candidates_by_context` saturava o único shard do cluster de
   desenvolvimento (`--smp 1`), com `WriteTimeout` do próprio coordenador —
   não um problema de timeout do cliente. A correção foi agrupar por
   partição `(context_id, user_id)` e escrever cada uma como um `BATCH
   UNLOGGED`, caindo para poucos milhares de requisições.

## Etapa 5 — OpenSearch (BD-4), incluindo a segunda célula inviável <a id="etapa-5-opensearch"></a>

Por [DESIGN.md](DESIGN.md), E-3 (pré-materialização) **não tem sentido
arquitetural** aqui — pré-materializar respostas chave-valor não aproveita
nada de um índice invertido. `storage/opensearch.py` não implementa
`get_prematerialized`, e `supported_primitives` não inclui essa primitiva.

**Modelagem**: um documento por (usuário, item) no índice `candidates`, com
`context_ids` como campo numérico multi-valor (desnormalizado na carga — é o
que a query `term` de E-2 resolve). Há ainda o índice `item_contexts`, um
documento por item (~87.585), lido em massa na montagem da célula para o
catálogo em memória. `get_candidates_filtered` usa uma bool query com uma
cláusula `term` por `context_id` pedido — interseção AND resolvida
nativamente pelo índice invertido do Lucene, sem workaround (E-2 "nativo").
Diferente de Valkey/Scylla, cada documento pertence só a um usuário — não há
estrutura global compartilhada entre usuários, então um `user_id` fora da
faixa real já isola os testes de integração, sem o risco de colisão que
apareceu nas etapas anteriores.

`intersect` usa a mesma técnica de consulta de `get_candidates_filtered` —
e aqui isso **não** é um placeholder a ser substituído mais tarde (diferente
da decisão tomada para Postgres, onde `intersect` reaproveita a SQL de E-2
só até a técnica `intarray`/roaring entrar em cena). Em OpenSearch, um
filtro `term`/`bool filter` já É a interseção de listas invertidas — não
existe, neste motor, uma segunda primitiva nativa para intersectar o
candidato com a lista invertida do contexto que seja distinta de aplicar o
predicado. E-2 e E-4 são, portanto, **mecanisticamente idênticos** nesta
tecnologia: resultado documentado da matriz de viabilidade (ver
[DESIGN.md](DESIGN.md), nota de rodapé da matriz), não uma lacuna de
implementação.

## Fase 2.6 — Catálogo item→contexto em memória (correção de assimetria em E-1) <a id="fase-2-6"></a>

Correção de uma ameaça à validade descoberta ao estimar a capacidade de uma
`n2-standard-8`, antes da bateria de medição. **Nenhuma latência foi medida
para chegar a ela** — a assimetria é visível na contagem de operações.

**O problema.** E-1 ("filtro na aplicação") precisa da pertença item→contexto
para avaliar o predicado. O contrato antigo fazia cada adaptador reconstruí-la
por requisição, e o custo disso era ditado pelo modelo de dados escolhido no
adaptador, não pela tecnologia sob teste. Para um usuário com 500 candidatos,
uma requisição custava:

| Adaptador | Round-trips | Operações no servidor | Onde o "join" acontecia |
|---|---|---|---|
| OpenSearch | 1 | 1 query + fetch de 500 `_source` | no tempo de indexação (desnormalizado) |
| PostgreSQL | 1 | `LEFT JOIN` + `array_agg` sobre ~1.500 linhas | dentro do planner |
| Valkey | 2 | **501 comandos** numa thread única | N+1, no cliente |
| ScyllaDB | ~9 | **501 consultas CQL** (janela de 64 ⇒ ~8 ondas) | N+1, no cliente |

Medida assim, a linha E-1 da matriz compararia **qualidade de adaptador**, não
tecnologia. O argumento de que "Valkey e Scylla não têm `JOIN`, logo N+1 é
inerente" não se sustenta: o OpenSearch também não tem, e não paga nada porque
desnormalizou na carga. Nada impedia os outros dois de fazerem o mesmo — a
normalização foi uma escolha do adaptador.

**A correção.** A pertença item→contexto é dado de catálogo: estático,
O(itens) (~87.585 itens, ~200 mil pares), sem ranking. Passou a ser carregada
**uma vez, na subida do serviço**, e mantida em memória (`core/catalog.py`,
~20-40 MB por worker). `Candidate` carrega só `(item_id, score)`; a pertença
nunca viaja no caminho quente. Isso é literalmente o que a definição de E-1
pede — "avalia o predicado no processo do serviço" — e é o que a docstring do
próprio contrato já dizia ser a intenção, antes de código e documento
divergirem.

Não confundir com E-3: a pré-materialização de E-3 é O(usuários × contextos) e
contém ranking; o catálogo é O(itens) e não contém nenhum.

**O que mudou:**

- `core/catalog.py` (novo) — `ItemCatalog`, com o predicado AND.
- `core/contract.py` — `Candidate` perdeu `context_ids`.
- `storage/base.py` — primitiva `load_item_contexts` (carga, não requisição).
- `strategies/base.py` — gancho `prepare(storage)` no protocolo, mais
  `build_cell_runtime` (checagem de compatibilidade + carga, em um lugar só).
- Os 4 adaptadores — `get_candidates` virou leitura pura; cada um ganhou seu
  despejo de catálogo (`GROUP BY` no Postgres, `SCAN`+`SMEMBERS` em lotes no
  Valkey, varredura paginada no Scylla, `search_after` no OpenSearch).
- OpenSearch ganhou o índice `item_contexts` (~87.585 documentos, poucos MB):
  reconstruir a pertença varrendo os ~100M documentos de `candidates` seria
  inviável, e o índice de catálogo custa quase nada.
- E-3 recebeu a mesma correção no caminho de contexto composto, que caía no
  filtro em aplicação de E-1.

**Efeitos colaterais bons:** o Postgres perdeu o `array_agg` e um `ORDER BY`
que era jogado fora (`core/ordering.py` reordena tudo de qualquer forma) —
E-1 ficou mais barato nas quatro tecnologias, não só nas duas penalizadas.

**Validação:** as 14 células viáveis continuam batendo com os 1000 casos do
oráculo (`make verify-all`, uma tecnologia por vez), e a camada rápida passa
com 207 testes. Testes novos de regressão garantem que o caminho de
requisição de E-1 chama **só** `get_candidates`, que o catálogo é carregado
uma única vez, e que `retrieve` sem `prepare` falha alto em vez de devolver
resposta vazia.

**Limitação registrada para a discussão de resultados:** o catálogo em
memória favorece E-1 frente a E-2/E-3/E-4, que continuam resolvendo o
predicado no banco. Isso é uma característica real da estratégia, não um
viés de implementação — mas precisa estar explícito na discussão dos
resultados.

## Etapa 6 — service/ <a id="etapa-6"></a>

`service/http_app.py` usa **Hypercorn**, não uvicorn (uvicorn não implementa
HTTP/2) — mesma disciplina já aplicada à troca Locust→k6 (ver Etapa 7). T-A
(HTTP/1.1) e T-B (HTTP/2 cleartext) são o **mesmo processo**: rodando sem
TLS, o Hypercorn aceita ambos automaticamente — não existe uma flag "ligar
HTTP/2" a configurar; a distinção vive inteiramente do lado do cliente de
carga. **Confirmado com um handshake HTTP/2 "prior knowledge" manual** (via a
biblioteca `h2` direto, sem passar por nenhum cliente HTTP de alto nível)
contra o serviço rodando: a resposta veio com `server: hypercorn-h2`,
provando que o HTTP/2 de verdade funciona, não só HTTP/1.1 disfarçado.

`service/grpc_app.py` + `service/proto/recommendation.proto` (T-C) estão
implementados e testados, mas não entram em `load/` ainda — a comparação de
transporte só faz sentido depois que a Fase 1 escolher a célula vencedora via
medição real na nuvem.

> `ScyllaAdapter.__init__` resolve/conecta no construtor (diferente de
> Postgres/Valkey/OpenSearch, que são preguiçosos) — decisão já tomada na
> Etapa 5. Por isso
> `service/tests/test_registry.py::test_build_storage_scylla_needs_no_credential`
> é `integration`, ao contrário dos testes equivalentes dos outros 3 bancos.

## Etapa 7 — load/ <a id="etapa-7"></a>

Gerador de carga contra o `service`. **k6**, não Locust: Locust é de malha
fechada (omissão coordenada quando o serviço degrada), k6 tem executor de
taxa de chegada constante (modelo aberto de verdade) e suporte nativo a gRPC.

`load/scenarios.js` usa o executor **`constant-arrival-rate`** — confirmado
via `k6 inspect` — com dois cenários por execução: `warmup` (2 min,
descartado) seguido de `measurement` (`startTime: '2m'`, 5 min), usando a tag
automática `scenario` do k6 (não timestamps) para a Etapa 8 filtrar o que
entra na análise. Um segundo modo (`RAMP_MODE=true`) troca para
`ramping-arrival-rate` com `abortOnFail` nos thresholds — implementa a
"rampa até violar o SLO" parando sozinho quando o SLO estoura, em vez de
rodar a rampa inteira.

`load/zipf.js` amostra `user_id` por Zipf (expoente 1,0, fixo pelo desenho
experimental) — sem lib externa no runtime do k6, a CDF é pré-computada uma
única vez via `SharedArray` (não recomputada por VU) e amostrada por busca
binária.

`load/export_contexts_by_tier.py` reaproveita `candidate_selectivity`/`tier`
de `data_generation/data/contexts.parquet` (calculados na Etapa 3, nunca
recalculados aqui) para gerar `load/fixtures/contexts_by_tier.json`, que o
k6 carrega via `SharedArray`/`open()` para escolher o único `context_id` do
patamar de seletividade pedido (`SELECTIVITY_TIER=high|medium|low`,
~2%/20%/60% dos candidatos sobrevivendo — os nomes dos patamares seguem a
convenção fixada em `data_generation/generator/config.py:TIER_TARGETS`:
"high" é seletividade *alta* = predicado mais restritivo = menos itens
sobrevivem, não o contrário).

**Limitação conhecida, documentada em `load/scenarios.js`:** o k6 negocia
HTTP/2 só sobre TLS (ALPN) — não fala h2c ("prior knowledge") em texto puro.
`service/http_app.py` já expõe h2c real (verificado manualmente na Etapa 6),
mas o k6 só vai conseguir exercitá-lo com TLS na frente do serviço — decisão
adiada para quando a Fase 2 (comparação de transporte) começar de fato. T-C
(gRPC) continua fora de `load/` pelo mesmo motivo já registrado na Etapa 6.

## Etapa 8 — analysis/ <a id="etapa-8"></a>

`analysis/collect.py` lê `requests.ndjson` (não `k6-raw.json`) e reconstrói
`latencies.parquet`. `requests.ndjson` é escrito por `console.log()` dentro
de `load/scenarios.js` (uma linha JSON já com os campos, um por requisição),
capturado via `k6 run --console-output=<arquivo> --log-format=raw`.

**Bug real encontrado e corrigido.** Isso substituiu uma tentativa anterior —
`returned_count` como métrica customizada (`Trend`) do k6, correlacionada com
`http_req_duration` pelo tag `request_id` — que quebrou em produção: tag com
valor único por requisição faz o motor de métricas do k6 registrar uma série
temporal nova a cada requisição, e uma bateria real (1000 req/s por minutos
contínuos) afundava o próprio processo k6 sob essa cardinalidade (p99 de
10-25s medido pelo k6 enquanto serviço e rede respondiam em ~1-2ms sob a
mesma carga, testada manualmente isolando cada camada). O k6 não tem suporte
estável a tag de alta cardinalidade não-indexada
(github.com/grafana/k6/issues/2584, em aberto) — logging estruturado em vez
de tag é a recomendação oficial do projeto para correlação por requisição.
`k6-raw.json` continua sendo gravado como saída nativa de diagnóstico do k6,
mas nada mais o lê.

`summary.json` é calculado em Python a partir do mesmo `latencies.parquet` —
não a partir do `handleSummary()` do k6 — para que percentis e o bootstrap de
`analysis/stats.py` usem exatamente o mesmo método de quantil, sem duas
implementações (uma em JS, outra em Python) que pudessem divergir. Só conta
como `measurement`/`ramp_to_slo` (nunca `warmup`, nunca `default` — este
último é a tag que o k6 usa quando `--vus`/`--duration` sobrescrevem os
cenários, como nos smokes locais).

## Fase 4 — Deploy em nuvem (GCP) <a id="fase-4"></a>

**Pegadinha real encontrada na prática — moeda do orçamento.**
`currency_code` precisa bater com a moeda da conta de faturamento (confira
com `gcloud billing accounts describe <conta>` — campo `currencyCode`). Se
divergir, `terraform apply` falha com "Error 400: Request contains an
invalid argument" — mensagem genérica que não menciona moeda em lugar
nenhum. Não é bug do Terraform nem da impersonação (testado e descartado):
reproduzido e isolado via `gcloud billing budgets create` direto, fora do
Terraform. `-var="currency_code=..."` precisa ser passado **de novo em cada
apply separado** — variáveis do Terraform não são "sticky" entre chamadas
(só o estado do recurso já aplicado é). Omiti-lo faria o Terraform tentar
reverter a moeda do orçamento para o default "USD", reproduzindo o mesmo
erro genérico.

**Trava de segurança de orçamento (Cloud Function).** A partir de 120% do
orçamento, uma Cloud Function desliga o billing do projeto inteiro
(`projects.updateBillingInfo` com `billing_account_name=""`) — não só as VMs
que o Terraform conhece, mas qualquer recurso que gere custo. `killswitch_dry_run`
tem default `true`: a function só loga "desligaria o billing agora" e para.
Ao testar o disparo sintético via Pub/Sub: **não pré-codifique o `--message`
em base64 você mesmo** — o próprio `gcloud pubsub topics publish` já faz isso
internamente (é assim que notificações reais de billing chegam); codificar
antes gera um "duplo base64" — a function decodifica uma vez e recebe de
volta a string base64 original, não o JSON, e quebra com `JSONDecodeError`
(confirmado numa execução real, a primeira a passar da barreira de IAM).

**Papéis IAM descobertos só na prática:** `roles/billing.projectManager`
sozinho não inclui `resourcemanager.projects.get`, que
`get_project_billing_info()` exige — precisou de `roles/browser` também,
descoberto só ao disparar a trava de verdade pela primeira vez.

**Recuperação depois de um disparo real**: religar a conta de faturamento
(`gcloud billing projects link <projeto> --billing-account=<conta>`) e
conferir manualmente o estado das VMs antes de retomar qualquer medição —
desligar o billing não deleta recursos, só os coloca em estado inconsistente
até o billing voltar.

## Fase 5 — Execução dos testes de carga <a id="fase-5"></a>

**Telemetria de gerador não medida vs. gerador ocioso.** `generator_cpu_unmeasured:
true` significa que ao menos uma sondagem ficou sem leitura de CPU do gerador
(Cloud Monitoring sem ponto na janela) — o portão dos 60% não pôde ser
avaliado ali. A vazão continua no relatório, mas não está validada nessa
dimensão. Medições **arquivadas antes desta mudança** gravavam uma falha de
telemetria como `generator_cpu_percent: 0.0`, indistinguível de "gerador
ocioso" — trate `0.0` em arquivo antigo como não medido (caso de
`results/e1-postgres/triagem/20260901T144228Z/`, com 0.0 nas 4 sondagens sob
1000 req/s).

**Instrumentação OpenTelemetry — a parte menos testada do projeto.** O Ops
Agent oficial do Google não roda em COS (sem gerenciador de pacotes); nada do
pipeline de métricas foi validado contra uma VM real antes de `--verify-otel`
existir, só `terraform validate`, que não pega esse tipo de falha. Rode
`--verify-otel` na primeira célula da triagem apenas — o módulo Terraform é
idêntico nas 14 células, então um OK vale para todas.
