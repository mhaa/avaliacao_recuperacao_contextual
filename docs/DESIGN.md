# Desenho experimental — recuperação contextual em APIs de recomendação

Este arquivo é a fonte de verdade sobre o desenho do experimento. Toda decisão de
implementação deve ser coerente com o que está aqui. Se algo precisar mudar, altere
este arquivo primeiro.

## Problema

Um sistema de recomendação gera, em processamento offline diário, um ranking de N
candidatos por usuário. Em tempo de requisição, o usuário navega em um contexto
(categoria, faixa de preço, disponibilidade, itens já vistos na sessão) que restringe
quais candidatos permanecem elegíveis. A resposta é o subconjunto do ranking que
satisfaz o predicado.

Isso inviabiliza cachear a resposta final: a cardinalidade de (usuário × contexto)
torna a taxa de acerto desprezível. O desafio se desloca para a camada de recuperação.

**Fora de escopo:** qualidade das recomendações, treino de modelos, re-ranqueamento
por inferência online, busca vetorial aproximada, execução sem servidor, bancos
gerenciados proprietários.

## Parâmetros fixos

| Símbolo | Valor | Significado |
|---|---|---|
| N | 500 | candidatos armazenados por usuário |
| k | 20 | itens retornados por requisição |
| C | 20 | cardinalidade de contextos (combinações de 1–2 gêneros do MovieLens) |
| I | 87.585 | itens no catálogo |
| U | 10.000 (local/smoke) / 200.948 (nuvem, medição principal) / 1.000.000–3.000.000 (nuvem, varredura de escalabilidade opcional) | usuários |

Dataset: MovieLens 32M (200.948 usuários, 87.585 filmes, ~32M avaliações).
Ranking offline gerado uma única vez por fatoração de matrizes (ALS, biblioteca
`implicit`). O algoritmo não é variável do estudo — o mesmo ranking vai para todas
as células.

## Fase 1 — Recuperação contextual (matriz 4 × 4)

### Estratégias

| Ref | Nome | Operação |
|---|---|---|
| E-1 | Filtro na aplicação | lê os N candidatos, avalia o predicado no processo do serviço |
| E-2 | Predicado delegado ao banco | envia o predicado, o banco retorna só os elegíveis |
| E-3 | Pré-materialização por contexto | leitura direta por chave (usuário, contexto) |
| E-4 | Interseção de conjuntos | intersecta candidatos do usuário com lista invertida do contexto |

Em E-4 as listas invertidas de contexto são **globais** (compartilhadas por toda a
base), portanto seu custo não escala com o número de usuários.

#### Catálogo item→contexto residente na aplicação (decisão de E-1/E-3)

A pertença item→contexto é **dado de catálogo**: estático, de tamanho O(itens)
(87.585 itens × C=20 contextos, ~200 mil pares), igual para todos os usuários e
sem nenhuma informação de ranking. E-1 precisa dele para avaliar o predicado no
processo do serviço; E-3 precisa dele no caminho de contexto composto, onde cai
para leitura completa + filtro em aplicação.

**Esse mapa é carregado uma vez, na subida do serviço, e mantido em memória** —
não é consultado ao banco a cada requisição. Consequência para o contrato de
`storage/`: `get_candidates` devolve apenas `(item_id, score)`; a pertença ao
contexto nunca viaja no caminho quente.

Justificativa. Sem isso, cada adaptador reconstrói `context_ids` por requisição
a um custo ditado pelo *modelo de dados escolhido no adaptador*, não pela
tecnologia sob teste: PostgreSQL resolvia com `LEFT JOIN` + `array_agg` sobre
~1.500 linhas (1 round-trip), OpenSearch de graça (desnormalizado no documento
em tempo de indexação), enquanto Valkey emitia 501 comandos numa thread única e
ScyllaDB 501 consultas CQL em ~8 ondas sequenciais. Medido assim, a linha E-1 da
matriz compararia qualidade de adaptador, não tecnologia — atribuindo a Valkey e
ScyllaDB um custo de normalização que nada na arquitetura deles impõe (o próprio
OpenSearch, que também não tem `JOIN`, não paga esse custo porque desnormalizou
na carga).

Não confundir com E-3: a pré-materialização de E-3 é O(usuários × contextos) e
contém ranking; o catálogo é O(itens) e não contém nenhum. Carregá-lo na
aplicação é o que a definição de E-1 ("avalia o predicado no processo do
serviço") já pede — não é pré-materialização disfarçada.

Custo: ~20-40 MB por processo de serviço (4 workers Hypercorn numa
`n2-standard-4` de 16 GB). O volume ocupado no banco não muda — o catálogo
continua normalizado em cada tecnologia, apenas deixa de ser lido por
requisição. A leitura em massa é a primitiva `load_item_contexts`, chamada uma
única vez na montagem da célula, nunca no caminho de requisição.

### Tecnologias

| Ref | Paradigma | Tecnologia |
|---|---|---|
| BD-1 | Relacional orientado a linhas | PostgreSQL |
| BD-2 | Chave-valor em memória | Valkey |
| BD-3 | Wide-column distribuído | ScyllaDB |
| BD-4 | Índice invertido | OpenSearch |

### Matriz de viabilidade (confirmada na implementação)

|  | BD-1 Postgres | BD-2 Valkey | BD-3 Scylla | BD-4 OpenSearch |
|---|---|---|---|---|
| E-1 | viável | viável | viável | viável |
| E-2 | viável (SQL + GIN) | viável (script Lua) | viável (clustering key) | viável (nativo)¹ |
| E-3 | viável | viável | viável (chave composta) | sem sentido arquitetural |
| E-4 | viável (intarray/roaring) | viável (SINTERSTORE) | **inviável** (sem primitiva) | viável (interno ao Lucene)¹ |

Células inviáveis são **resultado documentado**, não falha. O motivo técnico de
cada uma está registrado na implementação (`storage/scylla.py`, `storage/opensearch.py`)
e verificado por teste (`tests/acceptance/test_infeasible_cells_fail_at_startup.py`)
— ver [ARCHITECTURE.md](ARCHITECTURE.md) e [DECISIONS.md](DECISIONS.md#etapa-5-scylla)
(E-4/Scylla) e [DECISIONS.md](DECISIONS.md#etapa-5-opensearch) (E-3/OpenSearch).

¹ **E-2 e E-4 são mecanisticamente idênticos em BD-4 (OpenSearch).** Não é
uma lacuna de implementação a fechar depois — é consequência estrutural do
motor ser um índice invertido: um filtro `term`/`bool filter` já É a
interseção de listas invertidas (postings lists) do Lucene. "Delegar o
predicado ao banco" (E-2) e "intersectar o candidato com a lista invertida do
contexto" (E-4) resolvem na mesma primitiva de execução — os rótulos
"nativo" e "interno ao Lucene" descrevem essa mesma operação por ângulos
diferentes (ótica da estratégia vs. ótica do motor), não duas técnicas
distintas. Isso contrasta com BD-1 e BD-2, onde as duas estratégias usam
técnicas genuinamente diferentes (Postgres: predicado via `WHERE`/`JOIN` vs.
interseção via `intarray` sobre uma lista invertida global materializada
por contexto, `inverted_lists`; Valkey: predicado via script Lua vs.
interseção via `SINTER` sobre `inverted:{context_id}`) — em nenhum dos dois
bancos E-2 e E-4 compartilham plano de execução. Nesse nível ("conjunto
materializado + primitiva nativa de intersecção, buscado por chave"),
Postgres/`intarray` e Valkey/`SINTER` ficam abaixo do OpenSearch, onde a
intersecção acontece DENTRO do índice (Lucene percorre postings lists via
skip-list) — mas ainda assim é uma primitiva de intersecção de conjuntos
nativa do banco, não predicado avaliado linha a linha. Ver
`storage/postgres.py`, `storage/opensearch.py` e
[DECISIONS.md](DECISIONS.md#etapa-4) /
[DECISIONS.md](DECISIONS.md#etapa-5-opensearch).

## Fase 2 — Camada de transmissão

Fixa a melhor configuração da Fase 1 e varia só a entrega. Cadeia em que cada par
adjacente isola um fator:

| Ref | Transporte | Serialização | Contraste isolado |
|---|---|---|---|
| T-A | HTTP/1.1 | JSON | referência |
| T-B | HTTP/2 | JSON | efeito do transporte (vs. T-A) |
| T-C | HTTP/2 | Protobuf | efeito da serialização (vs. T-B) |

Varredura complementar: k em 20, 100 e 500, para localizar a partir de que tamanho
de resposta a serialização binária compensa.

## Contrato da API

Requisição: `user_id`, `context[]` (atributos categóricos), `exclude[]` (itens já
vistos), `k`.

Resposta: lista ordenada de k itens, cada um com `item_id`, `score`, `rank`.
**Sem metadados descritivos** — evita join e mantém o payload em ~800 bytes.

## Protocolo de medição

- Carga: 100, 1.000 e 10.000 req/s.
- SLO: p99 > 200 ms ou taxa de erro > 1%.
- Seletividade do predicado: ~2%, ~20%, ~60% dos candidatos sobrevivem.
- Distribuição de acesso: Zipf com expoente 1,0 (não uniforme).
- Modelo aberto (taxa de chegada constante), para evitar omissão coordenada.
- 2 min de aquecimento descartados + 5 min de medição (carga fixa).
- 5 repetições por célula, em ordem aleatorizada.
- Gerador de carga em instância separada; **válida só se CPU do gerador < 60%,
  nas duas rampas abaixo** — se o gerador saturar antes da célula, a execução é
  inválida (limite do gerador, não da célula) e exige escalar a instância antes
  de repetir (`load/saturation.py:GENERATOR_CPU_THRESHOLD`).

### Vazão de saturação (3ª dimensão da fronteira de Pareto)

Além de latência e custo, a triagem também busca até que vazão cada célula
sustenta antes de violar o SLO — os gargalos diferem por estratégia (E-1
tende a saturar por rede/CPU da aplicação; E-2 relacional, por CPU do banco;
E-3, por memória), então uma célula que perde em latência sob carga fixa
pode ainda assim saturar mais tarde, o que a tornaria mais barata em
produção (menos instâncias para a mesma demanda).

**Rampa curta (triagem, exploratória)** — `load/saturation.py`:
- 1 repetição por patamar (ensaio único; nunca entra em nenhuma tabela final —
  só decide a fronteira).
- Patamares grosseiros dobrando a partir de 1.000 req/s (1k, 2k, 4k, 8k,
  16k, ...), 1 min de permanência por patamar, sem aquecimento.
- Ao violar o SLO: busca binária entre o último patamar válido e o que
  violou, até 3 iterações de 1 min cada.
- Seletividade fixa no nível intermediário, igual à da carga fixa.
- Duração alvo: 10-15 min por célula no caso típico.
- **Teto de 50.000 req/s.** Se a célula não violar o SLO nem no teto (e o
  gerador seguir abaixo de 60%), a vazão fica **censurada**:
  `saturation_censored=true`, `saturation_lower_bound=50000` — não é o
  valor medido, é só "sabemos que é pelo menos isso".

**Rampa de confirmação** — só nas células não dominadas (fronteira):
- 5 repetições por patamar, em ordem aleatorizada.
- Patamares finos: incrementos de 10% na vizinhança do valor aproximado
  obtido na triagem (ou do `saturation_lower_bound`, se a célula ficou
  censurada).
- 2 min de aquecimento descartado + 3 min de medição por patamar.
- Roda nos três patamares de seletividade.
- Saída com distribuição completa (não só o ponto de violação) e intervalo
  de confiança por bootstrap.

**Atribuição de gargalo** — durante a rampa de confirmação, `resources.csv`
registra CPU/memória/rede das 3 VMs (banco, serviço, gerador) a cada 5
segundos, para identificar qual recurso satura primeiro
(`analysis/resources.py:classify_bottleneck`). Transforma "a célula satura em
11.000 req/s" em "satura em 11.000 req/s por CPU do banco" — o que entra na
discussão de resultados.

### Métricas

p50, p95, p99, p99,9 da latência · vazão atendida · vazão de saturação
(aproximada ou censurada) · CPU, memória e rede (banco/serviço/gerador),
com atribuição de qual recurso satura primeiro · taxa de erro · taxa de
acerto de cache · volume de armazenamento ocupado.

**Nunca reportar latência média.** A distribuição é assimétrica; a média esconde a cauda.

### Estatística

Kruskal-Wallis; se rejeitar H0, Dunn com correção de Bonferroni (α = 5%).
Intervalos de confiança dos percentis por bootstrap com 10.000 reamostras.
Reportar também a magnitude do efeito.

## Delineamento em duas etapas

1. **Triagem** — todas as células viáveis, com seletividade e carga fixas em nível
   intermediário, mais a rampa curta de saturação. Identifica a fronteira de
   Pareto em **3 dimensões**: latência × custo × vazão de saturação
   (`analysis/pareto.py`).
2. **Confirmação** — só as células da fronteira, com varredura completa de
   seletividade e carga e as 5 repetições, mais a rampa de confirmação.

### Regra de tolerância na dominância (vazão de saturação)

A rampa curta tem repetição única, sem estimativa de variância — por isso,
na comparação de dominância, células cuja vazão de saturação difira em
menos de 20% são tratadas como **equivalentes** nessa dimensão, nunca
descartando a vencedora por ruído de medição (o custo de levar uma célula a
mais para a confirmação é muito menor que o de perder a certa). Células
**censuradas** (não saturaram nem no teto) são equivalentes entre si nessa
dimensão e superiores a qualquer célula não censurada — nunca o teto é
usado como se fosse o valor real medido.

Se mais da metade das células viáveis ficar censurada, isso é sinalizado
explicitamente: indica que a dimensão de vazão não discriminou as
configurações neste delineamento, e a fronteira deveria ser reduzida a
latência × custo (2D).

## Hipóteses

- **H1** — E-3 tem a menor latência de cauda, ao custo de armazenamento proporcional
  a U × C × k.
- **H2** — a vantagem de E-2 sobre E-1 é função da seletividade, invertendo-se abaixo
  de um limiar.
- **H3** — cachear o conjunto de candidatos por usuário tem taxa de acerto muito
  superior a cachear a resposta completa, sem violar a corretude.

E-3 vence em armazenamento enquanto **C × k < N**. Com os valores atuais
(20 × 20 = 400 < 500), E-3 ocupa menos que E-1. Acrescentar um segundo eixo
contextual inverte isso. Esse cruzamento é um resultado central do trabalho.

## Regra de ouro da implementação

Antes de medir latência, **provar que todas as células devolvem exatamente o mesmo
resultado** para a mesma entrada, incluindo a ordem. Sem isso, comparar tempo não
significa nada — pode-se estar premiando a implementação que devolve menos itens.

Manter um conjunto de casos de teste com resposta esperada (oráculo) e uma
verificação automática que roda contra todas as células.

## Ambientes

- **Local** (Docker Compose): implementação, correção, U = 10.000. Nunca medir aqui.
- **Nuvem — smoke test** (Terraform, região única): valida a infraestrutura antes de
  qualquer bateria real; usa a mesma massa de desenvolvimento (U = 10.000) gerada para
  o ambiente local, só que subida e executada na nuvem.
- **Nuvem — medição principal** (Terraform, região única): U = 200.948, a base real
  completa do MovieLens 32M, sem amostragem. É esta rodada que produz os dados finais
  (Fases 1 e 2, as 14 células).
- **Nuvem — varredura de escalabilidade** (opcional, só na célula vencedora da medição
  principal): expande a base para U ∈ {1.000.000, 3.000.000} usuários sintéticos via
  reamostragem de perfis reais com ruído de popularidade injetado
  (`generator synthetic --scale`, ver `data_generation/generator/synthetic.py`), para
  observar como a latência escala além do tamanho real do dataset. Não é a fonte dos
  resultados principais.

### Equivalência de infraestrutura entre células

Justiça de comparação exige que as 14 células rodem sobre **hardware idêntico** — a
diferença de desempenho medida deve vir da estratégia/tecnologia sob teste, nunca de
uma célula ter ganhado mais máquina que outra.

**Hardware — idêntico entre as 14 células, sem exceção** (verificado no código, não
assumido: nenhum `cells/*.yaml` nem `infra/envs/experiment` sobrescreve estes
valores — todas usam o default de `infra/modules/*`):

| VM | Tipo | vCPU / RAM |
|---|---|---|
| banco | `n2-standard-8` | 8 / 32 GB |
| serviço | `n2-standard-4` | 4 / 16 GB |
| gerador de carga (loadgen) | `n2-standard-8` | 8 / 32 GB |
| disco de dados | `pd-ssd`, 200 GB | — |

**Configuração interna de cada banco — deliberadamente diferente, mesma fração
idiomática da máquina.** Números brutos idênticos (ex.: "2 GB de heap para todos")
não seriam justos entre arquiteturas de memória tão diferentes; a comparação correta
é "cada tecnologia configurada segundo sua própria prática recomendada, para usar a
mesma máquina disponível":

| Tecnologia | Alocação | Fração da VM de 32 GB | Motivo |
|---|---|---|---|
| ScyllaDB | `--smp 7 --memory 28G` | 87,5 % explícito (CPU e RAM) | Motor Seastar shard-per-core exige declaração explícita de CPU/memória — não existe modo "usa o que sobrar" |
| OpenSearch | heap JVM `-Xmx16g` | 50 % explícito + o resto via cache de página do SO para os índices Lucene (off-heap) | Teto recomendado pelo próprio Elasticsearch/OpenSearch — acima disso perde-se o benefício do *compressed oops* da JVM |
| PostgreSQL | `shared_buffers=8GB` | 25 % explícito + o resto via cache de página do SO | Orientação padrão de tuning do Postgres — o SO já compensa um `shared_buffers` menor |
| Valkey | `--maxmemory 24gb` | 75 % explícito, mas uso de CPU inerentemente de uma só thread para o caminho principal de dados | Característica arquitetural do Valkey/Redis, não sub-provisionamento — não há como "forçar" uso de mais núcleos sem descaracterizar a tecnologia |

**Lição de implementação (relevante para a seção de limitações/ameaças à validade):**
as configurações de Scylla e OpenSearch foram inicialmente herdadas, inalteradas, do
`docker-compose.yml` de desenvolvimento local (`--smp 1 --memory 2G` e heap
`-Xmx2g`, respectivamente) — valores adequados para correção em U = 10.000
local, mas que sub-provisionavam severamente a VM de nuvem real (`n2-standard-8`,
32 GB). Isso só foi descoberto rodando a bateria de medição real pela primeira vez
contra cada tecnologia: o Scylla usava consistentemente ~13 % de CPU (1 de 8 núcleos)
e levava mais de uma hora para carregar a base completa, quando deveria ser questão
de minutos — sintoma visível só em execução real, não capturado por nenhuma validação
estática. Reforça a prática já adotada no projeto de nunca confiar em configuração
copiada do ambiente local de correção para o ambiente de medição de desempenho, e de
sempre confirmar contra uma execução real antes de aceitar um resultado de latência
como válido.

## Pilha

Python · FastAPI · Hypercorn · grpcio · psycopg 3 · valkey-py · cassandra-driver ·
opensearch-py · polars · numpy · scipy · implicit · pyroaring · k6 ·
scikit-posthocs · matplotlib · Terraform · google-cloud-monitoring ·
OpenTelemetry Collector (instrumentação de gargalo, VMs COS)

Sobre o gerador de carga: k6 no lugar de Locust — Locust é nativamente de malha
fechada, o que produz omissão coordenada quando o serviço degrada sob carga; o k6
tem executor de taxa de chegada constante (modelo aberto de verdade) e suporte
nativo a gRPC, usado em `load/scenarios.js`.

Sobre o servidor HTTP: uvicorn não implementa HTTP/2, exigido por T-B. Hypercorn
serve T-A (HTTP/1.1) e T-B (HTTP/2 cleartext) a partir do mesmo app FastAPI, sem
processo/proxy adicional cujo uso de CPU teria que ser contabilizado à parte na
medição de recursos do serviço — evita confundir a comparação de transporte com um
componente extra na infraestrutura.

## Ordem de trabalho

1. Dimensionamento de armazenamento e custo (antes de provisionar qualquer coisa)
2. Gerador de massa sintética
3. Esquemas nos quatro bancos
4. Serviço com as quatro estratégias
5. Arnês de correção (oráculo)
6. Scripts de carga
7. Coleta e análise estatística
8. Terraform
9. Medição na nuvem
