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

Custo: ~20-40 MB por processo de serviço (8 workers Hypercorn numa
`n2-standard-8` de 32 GB). O volume ocupado no banco não muda — o catálogo
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

- Carga: 100 e 1.000 req/s fixos, mais um terceiro nível "alto" que, na
  confirmação, deixou de ser um valor fixo (era 10.000 req/s) — passa a ser a
  vazão de saturação aproximada medida pela própria rampa de confirmação
  daquela seletividade (uma por seletividade, ver "Rampa de confirmação"
  abaixo). Motivo: 10.000 req/s já estava bem além do ponto de saturação real
  das células medidas, testando só a região de falha profunda (violação de
  SLO praticamente garantida) em vez de um ponto que discrimine células.
- SLO: p99 > 200 ms ou taxa de erro > 1%.
- Seletividade do predicado: ~2%, ~20%, ~60% dos candidatos sobrevivem.
- Distribuição de acesso: Zipf com expoente 1,0 (não uniforme), **sobre a base
  de usuários inteira do ambiente** (U da tabela de parâmetros). `load/zipf.js`
  não conhece U — o orquestrador injeta `USER_COUNT` no k6 em toda execução de
  medição (`load/run_battery.py` recusa rodar sem `--user-count` fora do modo
  smoke); confiar no default do script já fez, uma vez, a nuvem amostrar só
  10.000 dos 200.948 usuários.
- Modelo aberto (taxa de chegada constante), para evitar omissão coordenada.
- **Vazão ofertada verificada, não presumida.** O modelo aberto só se sustenta
  enquanto o k6 tem VUs livres: quando a célula degrada a ponto de esgotar
  `maxVUs`, o k6 descarta chegadas e as latências registradas passam a ser só
  das requisições sobreviventes (omissão coordenada reaparecendo pela porta dos
  fundos). Por isso toda repetição/sondagem compara o volume registrado com o
  esperado da taxa-alvo: abaixo de 95%
  (`analysis/collect.py:MIN_OFFERED_RATIO`), uma sondagem de saturação conta
  como violação do SLO (o patamar não foi de fato oferecido) e uma repetição de
  carga fixa é marcada em `summary.json` (`offered_ratio`,
  `offered_load_ok=false`) — as latências dela nunca devem ser lidas como se o
  modelo aberto tivesse sido mantido.
- 2 min de aquecimento descartados + 5 min de medição (carga fixa).
- 5 repetições por célula, em ordem aleatorizada.
- Gerador de carga em instância separada; **válida só se CPU do gerador < 60%,
  nas duas rampas abaixo** — se o gerador saturar antes da célula, a execução é
  inválida (limite do gerador, não da célula) e exige escalar a instância antes
  de repetir (`load/saturation.py:GENERATOR_CPU_THRESHOLD`).

### Vazão de saturação (internalizada no custo, não um 3º eixo)

A triagem busca até que vazão cada célula sustenta antes de violar o SLO — os
gargalos diferem por estratégia (E-1 tende a saturar por rede/CPU da
aplicação; E-2 relacional, por CPU do banco; E-3, por memória), então uma
célula que perde em latência sob carga fixa pode ainda assim saturar mais
tarde, o que a torna mais barata por requisição: **mais capacidade pela mesma
unidade**.

Essa última frase é, literalmente, a fórmula de custo — e é por isso que a
vazão **não** é um eixo próprio da fronteira. `S` entra no DENOMINADOR do
custo (ver "Custo — por milhão de requisições"), logo o custo já depende
dela; mantê-la também como terceiro eixo contaria a mesma grandeza duas vezes
e distorceria a dominância. A fronteira é **2D: latência × custo por milhão
de requisições**.

Isso eleva a exigência sobre a precisão da busca: um erro em `S` vira erro no
custo. Daí o protocolo abaixo.

**Rampa curta (triagem)** — `load/saturation.py`:
- Patamares em incrementos de **25%** a partir de 1.000 req/s, **2 min** de
  permanência por patamar, sem aquecimento (antes: dobras de 1 min, que
  podiam pôr o ponto de violação ao dobro do valor real).
- Ao violar o SLO: busca binária entre o último patamar válido e o que
  violou, até **5** iterações. São elas, e não os passos finos, que
  determinam a resolução das células que saturam **abaixo** do rate inicial
  (violam já na primeira sondagem, e a busca desce de 0 até lá).
- **5 repetições do patamar final aprovado** (`final_level_probes` em
  `saturation.json`). É o que tira o `S` reportado da condição de ensaio
  único: permite reportar dispersão do p99 e quantas repetições violaram o
  SLO no mesmo patamar. As repetições **não** recalculam o valor aproximado.
- Seletividade fixa no nível intermediário, igual à da carga fixa.
- **Teto de 50.000 req/s.** Se a célula não violar o SLO nem no teto (e o
  gerador seguir abaixo de 60%), a vazão fica **censurada**:
  `saturation_censored=true`, `saturation_lower_bound=50000` — não é o
  valor medido, é só "sabemos que é pelo menos isso".
- Consequência da censura sob o modelo de custo: do fato medido `S ≥ L`,
  como o custo é DECRESCENTE em `S`, só se conhece um **teto** de custo por
  milhão de requisições (usando `L`) — nunca um piso, que exigiria um teto de
  `S` que não foi medido. Uma célula censurada nunca fica sem custo (entra no
  plano com esse teto), mas fica sem PONTO: nunca aparece em
  `cheapest_cell_ids`, e só domina/é dominada dentro do que o teto permite
  (`analysis/pareto.py`).

**Rampa de confirmação** — só nas células não dominadas (fronteira):
- Roda **antes** da bateria de carga fixa (100/1.000/"alto") — não é só uma
  questão de ordem: o resultado dela alimenta diretamente o nível "alto"
  dessa bateria (ver "Protocolo de medição" acima), então a dependência é de
  dados, não só de sequência.
- 5 repetições por patamar, em ordem aleatorizada.
- Patamares finos: incrementos de 10% na vizinhança do valor aproximado
  obtido na triagem (ou do `saturation_lower_bound`, se a célula ficou
  censurada).
- 2 min de aquecimento descartado + 3 min de medição por patamar.
- Roda nos três patamares de seletividade, uma rampa por seletividade —
  produz uma vazão de saturação por seletividade, não uma única para as três.
- Saída com distribuição completa (não só o ponto de violação) e intervalo
  de confiança por bootstrap.

**Re-medição de seletividade média (limitação encontrada e correção)** — a
decisão de SLO de cada sondagem da rampa fina poolizava as 5 repetições
(`CONFIRMATION_REPETITIONS`) num só dataframe antes de calcular UM p99
agregado (`analysis/probe_report.py`, `pl.concat` antes de `build_summary`).
Isso mascara variância real entre repetições: achado ao vivo em
e3-postgres/seletividade alta, vazão aprovada 4.365 req/s — p99 individuais
das 5 repetições = [222, 419, 246, 232, 334] ms (**todas** violam os 200ms
do SLO), mas o p99 do pool concatenado ficava ≤200ms, porque a proporção de
cada repetição na amostra combinada (~6,5 milhões de requisições) varia
pouco entre reamostragens — o mesmo efeito, generalizado, do IC bootstrap
sobre dados agrupados discutido na seção de intervalos de confiança. Na
prática, olhando as 12 combinações célula×seletividade já medidas, só 3
ficaram de fato perto de 200ms; as demais estouraram ou ficaram bem
conservadoras.

Correção, opt-in e retrocompatível (nunca reinterpreta o que já foi
publicado — triagem e as seletividades baixa/alta de todas as 4 células
continuam vindo do método antigo, "pooled"):
- `analysis/probe_report.py --decision-statistic median-per-repetition`:
  calcula p99/taxa de erro de CADA repetição separadamente e usa a
  **mediana** entre elas para decidir `violated_slo` (com N=5, "mediana >
  200ms" equivale a "maioria das repetições violam individualmente" — a
  pergunta que interessa, "essa vazão tipicamente quebra o SLO", não "o
  agregado poolizado quebra"). Portão de vazão ofertada usa o MÍNIMO entre
  repetições, não a mediana — é um portão de validade, não de desempenho,
  e mediana deixaria repetições degradadas se esconderem atrás de
  saudáveis. Default continua `pooled` (comportamento histórico).
- `infra/scripts/run_measurement_battery.py --tier {low,medium,high}`:
  restringe a rampa de confirmação e a bateria de carga fixa a UMA
  seletividade (default: as 3) — permite re-medir só a que precisa, sem
  gastar tempo/$ refazendo as outras duas. Não se aplica à triagem (já
  fixa em seletividade "medium").
- `--saturation-min-step`: a busca binária passa a parar por LARGURA de
  intervalo (req/s), não por um número fixo de iterações
  (`load/saturation.py:_binary_search`) — para quando se quer um `S` com
  precisão-alvo em vez de uma resolução implícita pelo número de
  iterações. Com essa mudança, a largura do bracket inicial passa a
  determinar o custo da busca — o que expôs outro problema: se a
  PRIMEIRA sondagem da rampa (`--saturation-start`, vindo da triagem
  antiga, método `pooled`, possivelmente otimista) já violar o SLO, o
  bracket entregue à busca binária seria `[0, start]` inteiro, não
  `[0.9×start, start]`. Corrigido com `_backward_walk_to_bracket`
  (`load/saturation.py`): nesse caso específico, a busca recua na mesma
  grade geométrica de 10% antes de entrar na busca binária, até achar um
  patamar que não viola — um teto de `BACKWARD_WALK_MAX_STEPS=10` recuos
  evita rodar indefinidamente numa célula degenerada. Só ativa quando
  `binary_search_min_step` está definido: no modo de iterações fixas
  (triagem, ou confirmação sem essa flag) a largura do bracket não muda o
  custo da busca, então recuar só somaria sondagens sem ganho.
- A rampa de confirmação confirma o patamar aprovado rodando-o no MESMO
  formato arquivado da combinação "alta" da bateria de carga fixa
  (`build_final_level_confirmation_commands`, reusa
  `build_remote_battery_command` — grava `manifest.json`, visível para
  `analysis/report.py`), em vez do mecanismo leve `confirm_repetitions`
  de `load/saturation.py:_confirm_final_level` (que grava só em
  `_saturation/`, nunca lido pelo relatório — esse mecanismo continua
  ativo só na rampa curta da triagem, com `confirm_repetitions=
  CONFIRMATION_REPETITIONS`; a confirmação passa `confirm_repetitions=0`
  agora). Achado ao vivo medindo e3-postgres: antes, o patamar final era
  medido DUAS vezes — uma pela confirmação leve (descartável) e outra pela
  bateria de carga fixa (arquivada) — 25 repetições reais no total (5 da
  confirmação, cada uma internamente 5 reps de `--decision-statistic
  median-per-repetition`). Agora são as MESMAS 5 repetições arquivadas que
  servem de dado real (para o relatório) e de trilha de dispersão
  (`final_level_probes` passa a ter 1 `ProbeResult` só, cujo próprio
  `per_rep_p99_ms`/`per_rep_violated_slo` já contém os 5 valores — não
  mais 5 `ProbeResult`s separados). Combinado com `--only-saturation`
  (pula a bateria de carga fixa inteira) e o reaproveitamento dos níveis
  100/1.000 já medidos na campanha original (inalterados pelo problema do
  `pooled`, que só afetava a decisão da BUSCA de saturação), a re-medição
  de uma célula passa a rodar só as sondagens da busca + 5 repetições no
  patamar alto — nenhuma repetição redundante.
- `saturation.json`/`saturation_<tier>.json` passam a registrar, por
  sondagem, `per_rep_p99_ms`/`per_rep_violated_slo` (trilha de auditoria de
  `--decision-statistic median-per-repetition`) — `None` no modo `pooled`
  ou em sondagens antigas, para o motivo de qualquer decisão futura ficar
  visível direto no artefato, sem precisar de investigação manual.
- Esses mesmos arquivos `saturation*.json` passam a ser enviados ao
  `results-bucket` (`gcloud storage cp` host→bucket, mesmo padrão já usado
  para `resources.csv`, logo após cada `_write_saturation_json`). Antes,
  ficavam só no host orquestrador — nunca passavam pela VM loadgen (só o
  `/app/results/<cell>` dela é sincronizado), então uma nova trilha de
  auditoria por repetição só sobreviveria na máquina que rodou a medição.
  Achado ao vivo verificando esta mesma campanha: a lacuna já era conhecida
  para `resources.csv` antes da correção documentada em "Atribuição de
  gargalo" abaixo, mas persistia sem correção para `saturation*.json`.

Status desta campanha: só a seletividade média, nas 4 células
(e2-scylla, e3-postgres, e3-valkey, e4-valkey), foi re-medida com o método
novo. Seletividade baixa e alta continuam valendo pela execução original.
e3-postgres/média (2026-09-21, S≈3980 req/s) rodou ANTES da otimização de
confirmação arquivada acima — sem `--only-saturation`, mediu 100/1.000/alto
de novo (dado válido, só redundante com o já existente). e2-scylla,
e3-valkey e e4-valkey usam `--only-saturation` com a confirmação já
arquivada, sem essa redundância.

**Atribuição de gargalo** — durante a rampa de confirmação, `resources.csv`
registra CPU/memória/rede das 3 VMs (banco, serviço, gerador), para
identificar qual recurso satura primeiro
(`analysis/resources.py:classify_bottleneck`). Transforma "a célula satura em
11.000 req/s" em "satura em 11.000 req/s por CPU do banco" — o que entra na
discussão de resultados. Amostrado a cada 30s, com uma janela de consulta de
150s (não 5s): o Cloud Monitoring recusa mais de 1 ponto por minuto para uma
métrica customizada (`workload.googleapis.com/*`, a família que o coletor
OpenTelemetry usa aqui — confirmado ao vivo, ver `infra/modules/database/
main.tf`), então uma janela mais estreita que esse piso de ~60s nunca contém
2 pontos para calcular uma taxa de CPU (achado ao vivo 2026-09-16: as 5
execuções da confirmação rodaram inteiras sem gravar uma única amostra por
essa causa — `analysis/resources.py`, docstring de `_CPU_METRIC`).

Rede (`compute.googleapis.com/instance/network/*`) é a única métrica aqui
nativa do Compute Engine, sem nenhum agente — mas, na prática, tem cadência
de emissão bem mais esparsa e menos previsível que as exportadas pelo OTel
Collector (achado ao vivo re-medindo e3-postgres/seletividade média,
2026-09-21: ~1 ponto a cada ~100s contra os ~5 pontos por janela de CPU/
memória). `GCPMonitoringCollector.collect()` tratava QUALQUER métrica
faltando como erro fatal da amostra inteira — então uma janela de 150s sem
nenhum ponto de rede descartava CPU e memória daquele instante junto,
mesmo as duas tendo chegado certas (visto ao vivo: dezenas de "AVISO: falha
ao amostrar recursos" seguidos, com CPU/memória disponíveis o tempo todo
via consulta direta). Corrigido: rede agora é best-effort, mesmo padrão já
usado para `memory_available_mb` — uma janela sem ponto de rede vira
`network_mbps=None` nessa amostra, sem derrubar CPU/memória.

### Experimento complementar — estresse com foco no banco

**Não faz parte da matriz 4×4.** Roda em hardware diferente, com objetivo
diferente, e seus números **nunca** entram na fronteira de Pareto nem no modelo
de custo. Existe para responder uma pergunta que o experimento principal, por
construção, não responde.

**Motivo.** `results/report/extra/resource_bottleneck_by_cell_tier.csv` registra
`bottleneck=service_cpu` em 12 de 12 combinações célula×seletividade medidas na
confirmação. A CPU do banco nunca passou de 21% (Scylla) e chegou a 0,9%
(Valkey). Normalizando por mil requisições, o serviço custa de 4× (Scylla) a 91×
(Valkey/E-3) o que o banco custa. Sob carga fixa (100/1.000 req/s) o p99 fica em
3-4 ms — o piso de serialização previsto em `docs/BENCHMARKS.md` §6. Ou seja: as
vazões de saturação de 1.325 a 4.711 req/s são propriedade do processo Python,
não das tecnologias de banco. Isso **não invalida** a matriz principal, cuja
pergunta é a latência da recuperação contextual ponta a ponta sob hardware
idêntico; apenas delimita o que ela mede.

**Desenho.** Para cada uma das 4 células já confirmadas (e2-scylla, e3-postgres,
e3-valkey, e4-valkey), com seletividade fixa no patamar médio:
- VM de serviço escalada em vCPUs (memória igual ou maior), dimensionada por
  célula até o teto arquitetural do banco correspondente — Scylla `--smp 7` →
  ~87,5% da VM; Postgres → 100%; Valkey, **thread única no caminho de dados** →
  ~12,5% de uma VM de 8 vCPU. A VM de banco **não muda**: é ela que se quer
  saturar.
- Gerador escalado junto: sob 11-38k req/s um `n2-standard-8` violaria o portão
  dos 60%, e a curva medida seria do gerador, não do banco.
- **Sondagem rápida do joelho, antes de montar a rampa.** Reaproveita
  `load/saturation.py:run_saturation_search` com patamares em dobra, **uma
  repetição por patamar** e busca binária para refinar — 9 a 12 sondagens de
  90 s, ~20-26 min. O objetivo é *enquadrar* o joelho, não reportá-lo com
  dispersão; a precisão vem depois, da fase fina construída em torno dele.

  Não é refinamento opcional: o cronograma da rampa é **estático** (execução
  única do k6), e sua fase fina cobre `[0,6·K, 1,3·K]`. Uma projeção errada em
  mais de ~40% para baixo joga a violação na fase grossa — resolução 10× pior —
  e gasta a fase fina inteira em sobrecarga, sem nenhum degrau de subida
  saudável e portanto sem veredito de recuperação. Simulado sobre e3-valkey com
  joelho real em 12.000 contra projeção de 38.300: primeira violação a 13.000
  req/s na fase grossa, 0 de 69 degraus finos saudáveis, `recovery=undetermined`
  — a campanha inteira paga sem produzir resultado. Sondar custa ~13% do tempo
  da rampa e elimina esse risco.

  **A sondagem desta campanha ignora a SLO de latência** (`--ignore-latency-slo`
  em `analysis/probe_report.py:violated_slo()`, `ignore_latency_slo=True` em
  `infra/scripts/run_stress_ramp.py:make_stress_probe_fn`) — só taxa de erro
  >1% (e o portão de vazão ofertada, nunca ignorado) decidem violação aqui,
  ao contrário da bateria principal, que usa a SLO real (p99>200ms OU erro
  >1%). Motivo: esta campanha quer o teto real do **banco**, não a
  experiência de cliente — e p99>200ms é um limiar de UX, não de capacidade.
  Achado ao vivo na 1ª sondagem de e4-valkey: CPU/memória agregados
  ficaram bem abaixo de qualquer teto quando o joelho "violou" por p99 —
  esperado, já que Valkey é single-thread no caminho de dados (~12,5% de
  CPU agregada quando saturado, ver "Valkey e o veredito automático" acima),
  mas levanta a suspeita de que o p99 do cliente pode estourar por fila em
  outra camada (serviço) antes do banco saturar de fato, subestimando o
  joelho que esta campanha existe para medir — daí a troca de critério.
  Decisão tomada antes de re-executar a sondagem com o critério novo;
  nenhum resultado de e4-valkey publicado até aqui usa `--ignore-latency-slo`
  (a sondagem que mediu 5.875 req/s foi descartada, refeita do zero).

  Três desfechos, tratados separadamente: gerador saturado (execução abortada —
  a rampa herdaria o teto do gerador), censurado (joelho acima de 50.000 req/s —
  usa-se o piso, marcado como piso) e o caso normal.
- Rampa contínua em degraus: fase grossa (+1.000 req/s a cada 30 s) até ~60% do
  joelho **medido**, fase fina (passo configurável, 1 min por degrau)
  atravessando o joelho **sem parar na primeira violação do SLO**, patamar em
  sobrecarga por 5 min, e **descida** pelos mesmos degraus.
- O artefato registra, em `knee`, onde a violação caiu e com que resolução —
  para que um joelho resolvido na fase grossa (projeção manual via `--knee`,
  ou sondagem pulada) nunca seja lido como se tivesse a precisão prometida.
- A descida é o instrumento: comparando degraus de mesma taxa na subida e na
  descida (vazão sustentada e p50/p95/p99), separa-se "o banco se recupera
  sozinho" de "o banco se perde". A assinatura de não-recuperação já apareceu
  espontaneamente em `results/e4-valkey/.../saturation_high.json` — p99 de
  3.987 ms ao voltar para 4.709 req/s, abaixo dos 4.695 req/s que eram limpos
  na subida.

**Sondagem linear rápida, opcional, fora do fluxo padrão.** `--quick-probe`
em `infra/scripts/run_stress_ramp.py` troca a sondagem do joelho + rampa
completa por uma varredura de +1.000 req/s a cada 30s
(`load/saturation.py:run_linear_probe_sweep`), subindo até a primeira
violação ou `--quick-probe-top` (default: 2x o joelho projetado de
`CELL_DEFAULTS`, ou 2x `--knee` se passado — `run_stress_ramp.py:_quick_probe_top`).
Existe para dois usos: mapear a curva inteira em resolução grosseira em
minutos em vez de horas, e checar rapidamente — via `db_cpu_cores.csv` — se o
dimensionamento atual do serviço (`CELL_DEFAULTS[cell]["service"]`) deixa o
BANCO aparecer como gargalo primeiro, antes de comprometer horas de VM numa
rampa completa. Usa o mesmo critério `--ignore-latency-slo` da sondagem do
joelho (só taxa de erro >1%, além do portão de vazão ofertada, nunca
ignorado). Não substitui nem a sondagem do joelho (que existe para
*enquadrar* um valor com busca binária) nem a rampa completa (que existe
para medir histerese via subida/descida) — é um terceiro instrumento, mais
barato e menos preciso, para checagem exploratória.

`--ignore-latency-slo` também passou a se aplicar ao veredito por degrau da
rampa COMPLETA (`analysis/ramp_report.py:step_results`), não só à sondagem do
joelho — mesma consistência metodológica em toda a campanha, mesmo escopo
(nunca a bateria principal, que nunca chama `ramp_report.py`).

Tanto a sondagem rápida quanto a rampa completa agora imprimem um veredito
explícito de gargalo (`analysis/resources.py:classify_bottleneck`, aplicado
sobre `resources.csv`) logo após a execução — "database_cpu"/"service_cpu"/
"loadgen_network" — em vez de só os tetos de memória usados no cálculo. Junto
dele, a CPU de pico de UM núcleo do banco (`db_cpu_cores.csv`) e um aviso
explícito quando o agregado aponta para outro componente mas o pico por
núcleo sugere saturação de thread única (Valkey) — o mesmo caso descrito em
"Valkey e o veredito automático" acima, agora superado automaticamente em vez
de exigir inspeção manual dos dois CSVs. A rampa completa também passou a
imprimir uma tabela por degrau (`infra/scripts/run_stress_ramp.py:format_ramp_step_table`),
com CPU/memória das 3 VMs correlacionadas por timestamp a cada degrau (as
mesmas colunas — em % da VM, nunca MB bruto — da tabela da sondagem rápida,
`format_quick_probe_table`), cobrindo subida, platô e descida por inteiro;
antes disso, o `RAMP_REPORT` de `analysis/ramp_report.py` era gerado
corretamente na VM do gerador mas seu stdout nunca chegava ao console de
quem roda a campanha, só ao JSON baixado depois. Ambas as tabelas renderizadas
também são salvas como artefato próprio (`quick_probe_table.txt`/
`ramp_<tier>_table.txt`, junto de `resources.csv`/`db_cpu_cores.csv`), não só
impressas — sobrevivem ao terminal fechar.

Ambas as tabelas ganharam três colunas logo após `rate`, para completar a
leitura de vazão sem obrigar quem lê a cruzar campos manualmente: `vazão`
(vazão realmente sustentada — `offered_ratio × rate` na sondagem rápida,
`throughput_rps` direto na rampa completa, já existente em `RampStepResult`),
`oferta%` (o mesmo `offered_ratio` em percentual — abaixo de 95% é o próprio
portão de vazão ofertada, `analysis/collect.py:MIN_OFFERED_RATIO`, que pode
ter violado o patamar mesmo com `err%=0,00`) e `vazãoSLO` (goodput: requisições
com `status<400` **e** `latência<=200ms` por segundo —
`analysis/probe_report.py:slo_throughput_rps` — distinta de `vazão`, que conta
toda requisição aceita, SLO ou não; um patamar pode ter vazão alta e vazãoSLO
baixa quando a fila cresce mas o k6 ainda não esgotou `maxVUs`). `vazãoSLO` só
é calculada no modo `--decision-statistic pooled` (o único que a campanha de
estresse usa) e nunca é lida pela bateria principal — `_ProbeVerdict`/
`ProbeResult`/`RampStepResult` ganharam o campo, mas triagem/confirmação não o
consomem.

**Duas limitações de instrumentação que o desenho precisa contornar**, ambas já
conhecidas do protocolo principal:
- `classify_bottleneck` calcula CPU como `1 - idle/total` **somada sobre todos os
  núcleos**. Um Valkey saturado lê ~12,5% numa VM de 8 vCPU, então o veredito
  automático nunca acusaria `database_cpu` nas células Valkey. Por isso a
  campanha amostra `/proc/stat` **por núcleo** na VM do banco.
- Essa mesma amostragem resolve a resolução temporal: o piso de ~60s do Cloud
  Monitoring (ver "Atribuição de gargalo" acima) borraria um único ponto sobre
  ~2,5 degraus de 1 min. `/proc/stat` a cada 5 s dá ~12 pontos por degrau.

**Leitura dos degraus além do joelho.** Passado o ponto de saturação,
`offered_ratio` cai abaixo de 0,95 e os percentis passam a ser só das
requisições sobreviventes — a omissão coordenada que "Vazão ofertada verificada,
não presumida" descreve. Aqui esse regime é o objeto de estudo, não um defeito:
os degraus não são descartados, mas ficam **marcados** no artefato e nas figuras,
e o p99 deles nunca deve ser lido como se o modelo aberto tivesse se mantido.

**Isolamento.** Estado Terraform, `TF_DATA_DIR`, nomes de recurso e namespace de
resultados são todos próprios (`_estresse/`, `ramp_<tier>.json`). O env
`infra/envs/experiment` e os artefatos `saturation*.json` da campanha principal
não são tocados nem relidos.

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
   Pareto em **2 dimensões**: latência × custo por milhão de requisições, com
   a vazão de saturação internalizada no custo (`analysis/pareto.py`). A
   fronteira é calculada uma vez — não depende de uma demanda externa — e
   segue inteira para a confirmação.
2. **Confirmação** — só as células da fronteira: primeiro a rampa de
   confirmação (uma por seletividade, determina o nível de carga "alto"),
   depois a varredura completa de seletividade e carga com as 5 repetições.

### Regra de tolerância na dominância (propagação da incerteza de S)

A rampa curta mede `S` com precisão limitada — por isso sobrevive uma
tolerância de 20%, alargando `S` para os dois lados antes de converter em
custo:

```
custo_lo = C(S·1,20)      custo_hi = C(S·0,80)
a domina b em custo  ⟺  custo_hi(a) < custo_lo(b)
```

(`C` é decrescente em `S` — quanto maior a vazão de saturação, menor o custo
por requisição — então o extremo ALTO de `S` dá o custo BAIXO, e vice-versa.)
Só há dominância em custo quando os intervalos são **disjuntos** — o que
preserva a intenção original (não descartar a vencedora por ruído; o custo de
levar uma célula a mais para a confirmação é muito menor que o de perder a
certa) sem precisar de um eixo separado.

A tolerância incide **apenas sobre `S`**, que é medido — nunca sobre o termo
de capacidade de memória, que é exato. Alargar um limite seria inventar
incerteza sobre um fato.

Células censuradas (`S ≥ L`, sem ponto medido) não recebem banda de
tolerância: o piso de custo fica em 0 (sem um teto medido de `S`, não há como
calcular um piso de custo honesto) e o teto vem de `L` diretamente — alargar
um limite em cima de outro limite seria inventar incerteza sobre um fato, o
mesmo erro que a tolerância evita para as não censuradas.

Se mais da metade das células viáveis ficar censurada, isso é sinalizado
explicitamente: para essas, o custo só tem um teto, nunca um ponto, e a
comparação pontual (`cheapest_cell_ids`) recai sobre as demais.

## Custo — por milhão de requisições, na capacidade máxima de uma unidade

Até a triagem das 14 células ser concluída, o custo era só **computação**
(soma dos tipos de VM), idêntico nas 4 tecnologias — que usam os mesmos tipos
de máquina. Isso deixou `custo` incapaz de discriminar qualquer célula: a
fronteira colapsou para 1 célula, decidida por ruído de milissegundo em
latência, porque `custo` (empatado) e `vazão` (dentro da tolerância de 20%)
não discriminavam nada.

O modelo atual corrige isso normalizando o custo mensal de UMA unidade de
atendimento pela capacidade máxima que essa unidade sustenta por mês — o
custo passa a ser expresso em **\$ por milhão de requisições**, não em \$
totais para uma demanda externa escolhida:

```
n   = ⌈V_mem / M⌉                        unidades (piso de memória; 1 em disco)
C_f = n · p_i · h                        fluxo    [p_i em $/hora, h = 730 h/mês]
C_a = n · V_disco · p_a                  estoque  [p_a em $/GiB-MÊS]
C   = (C_f + C_a) · 10^6 / (S · 2.592.000)   [$ por milhão de requisições]
```

Não existe mais uma demanda `D` externa a escolher: a análise sempre opera na
capacidade máxima de UMA unidade de atendimento. O `n` acima é só o piso de
memória (`⌈V_mem/M⌉` — ver "Ressalva obrigatória" abaixo), que hoje nunca
ultrapassa 1 nos dados reais. `2.592.000` são os segundos em 30 dias,
convertendo a vazão de saturação (req/s) em capacidade mensal (req/mês) —
nota: `h = 730` horas/mês (média, 30,42 dias) usa uma convenção mensal
ligeiramente diferente de `2.592.000` (30 dias exatos); a discrepância de
~1,4% entre as duas não muda nenhuma comparação (afeta todas as células
igualmente) — declarar no texto do TCC.

- **Unidade de atendimento** = 1 VM de banco (`n2-standard-8`) + 1 VM de
  serviço (`n2-standard-8`). A VM geradora **não** entra: é aparato de
  medição, não capacidade produtiva.
- **Sem sharding**: cada unidade mantém réplica integral da base — é por isso
  que `C_a` carrega o fator `n`. É a premissa mais contestável do modelo e
  precisa estar declarada no texto do TCC.
- **`p_a` é mensal**, não horário: `C_a` tem de sair na mesma unidade de
  `C_f = n·p_i·h`, ou a soma não significa nada.
- **Convenção de unidade**: bytes convertidos por 1024³ (GiB). A GCP rotula
  "GB" nas tabelas de preço mas fatura em potências de 2 — merece nota de
  rodapé no texto.

### Memória e disco entram por caminhos diferentes, de propósito

- **Disco** é elástico e faturado à parte → entra continuamente, por GiB, em
  `C_a`.
- **Memória** já está paga dentro de `p_i` (o `n2-standard-8` vem com 32 GB).
  Cobrá-la de novo por GiB seria **dupla contagem**. O que ela faz é
  **limitar quantas unidades cabem**: `⌈V_mem / M⌉`, com `M` = 24 GiB (o
  `--maxmemory 24gb` de `infra/modules/database/main.tf`). Para mecanismos em
  disco, `V_mem = 0`; para os em memória, `V_disco = 0`.

Esse arranjo evita os dois erros das alternativas: não conta a RAM duas
vezes, e não dá ao Valkey uma parcela de estoque nula que o tornaria
artificialmente o mais barato.

**Ressalva obrigatória**: com U = 200.948 o maior `V_mem` é ~3,5 GiB, logo
`⌈3,5/24⌉ = 1` — **o termo de capacidade não é acionado nos dados atuais**.
Ele só passa a discriminar em extrapolação para U maior, que é justamente
onde H1 (armazenamento proporcional a U×C×k) se manifesta em custo. Declarar,
ou o leitor supõe que o termo está fazendo um trabalho que ainda não faz.

Como o custo não depende mais de uma demanda externa, não há mais "pontos de
cruzamento" a detectar variando `D` — a fronteira é calculada uma vez.
**Empate de custo continua reportado como empate**: `cheapest_cell_ids` é uma
lista, não uma célula. Com memória fora do preço por GiB, células Valkey de
mesma estratégia têm custo por unidade idêntico ao centavo; desempatar por
ordem alfabética afirmaria "e1-valkey é a mais barata", o que seria artefato
de desempate, não resultado.

### Preços

Região do experimento: **`us-east4`**.

Regra adotada: para cada item, usar o número mais **diretamente publicado**
que existe, mesmo que isso implique fontes de regiões diferentes — o que é
declarado, não escondido.

- **Disco** (`pd-ssd`, `us-east4`): **$0,187/GiB-mês**. Fonte: tabela pública
  da GCP, consultada em 2026-09-06. Há tabela por região, então usa-se a da
  região do experimento.
- **Computação** (`n2-standard-4` = $0,1942/h, `n2-standard-8` = $0,3885/h):
  preço de referência das regiões **baseline dos EUA**, aplicado **sem**
  multiplicador regional. N2 não tem tabela pública quebrada por região, e
  derivar `us-east4` multiplicando pelo prêmio de ~8% produziria uma
  estimativa não conferida no lugar de um número citável. **Consequência a
  declarar no texto**: o custo é expresso em preço de referência dos EUA, não
  em preço específico de `us-east4` — que é cerca de 8% maior. Como o interesse
  do trabalho está na comparação entre configurações, e o preço de instância é
  idêntico nas quatro tecnologias, um deslocamento uniforme de escala não
  altera nenhuma conclusão; apenas os valores absolutos.

`dimensionamento.xlsx` (Etapa 1) segue válido como estimativa
pré-provisionamento; estes números são o que a própria planilha previa
substituir "após carga piloto" — a carga piloto já aconteceu.

**Ordem de grandeza que orienta a leitura**: a parcela de estoque fica entre
0,02% e ~2,5% do custo de uma unidade. Ou seja, a computação (`C_f`) domina o
numerador, e o armazenamento age como desempate fino. O que de fato
discrimina o custo por milhão de requisições entre células é a vazão de
saturação `S`, no denominador. Atribuir à ocupação de armazenamento um peso
que os dados não sustentam é o erro de leitura a evitar.

**Nota histórica — a razão memória:disco não é mais usada.** Uma versão
anterior deste modelo precificava memória por GiB e comparava as duas razões
(a planilha da Etapa 1 supunha memória ≈50× mais cara que disco; a consulta
real deu ≈16,5×). Essa comparação ficou obsoleta: memória deixou de ter preço
por GiB no modelo, justamente porque cobrá-la assim duplicaria o que `p_i` já
paga. Registrado aqui só para que a razão de ≈50× da planilha não reapareça
no texto do TCC como se ainda valesse.

**O que medir, por estratégia e tecnologia** — mapeado às tabelas/padrões de
chave/índices que cada adaptador realmente usa (`storage/*.py`), não ao
modelo idealizado da planilha original (que assume um deploy mínimo isolado
por estratégia):

| Estratégia | Postgres (tabela) | Valkey (padrão de chave) | ScyllaDB (tabela) | OpenSearch (índice) |
|---|---|---|---|---|
| E-1 | `candidates` | `candidates:*` | `candidates` | `candidates` |
| E-2 | `candidates` + `item_contexts` | `candidates:*` + `item_contexts:*` | `candidates_by_context` | `candidates` (mecanisticamente = E-4) |
| E-3 | `prematerialized` | `prematerialized:*` | `prematerialized` | inviável |
| E-4 | `candidates` + `inverted_lists` | `candidates_set:*` + `inverted:*` | inviável | `candidates` (= E-2) |

`item_contexts`/`catalog:item_contexts` é o catálogo compartilhado (~87.585
itens, pequeno), carregado uma vez na montagem — entra na conta de E-2
porque é o que o script Lua (Valkey) ou o `JOIN`/GIN (Postgres) consultam por
requisição; não entraria num deploy que só serve E-1 sozinho, já que E-1 usa
o catálogo em memória da aplicação (ver "Catálogo item→contexto residente na
aplicação" acima).

**Medição — real, não estimada por fator de sobrecarga.** Armazenamento não
varia com carga/taxa de requisição (é função só do dataset carregado), então
não exige reexecutar nenhuma bateria — só medir uma vez por tecnologia contra
a base já carregada:

- **Postgres**: `pg_total_relation_size(tabela)` por tabela — exato.
- **ScyllaDB**: `system.size_estimates` (tabela virtual do driver CQL) —
  `mean_partition_size × partitions_count` por tabela — exato o suficiente
  sem precisar de SSH/`nodetool`.
- **OpenSearch**: `_cat/indices?bytes=b` por índice — exato.
- **Valkey**: `INFO memory` → `used_memory` dá o total real da instância
  inteira, sem estimativa. Para quebrar por padrão de chave (necessário para
  diferenciar E-1/E-2/E-3/E-4 dentro da mesma instância): `SCAN` com `MATCH`
  por padrão + `MEMORY USAGE` amostrado sobre uma amostra de chaves daquele
  padrão, multiplicado pela contagem real de chaves (contada pelo próprio
  `SCAN`, não estimada). É a única das 4 tecnologias com uma componente de
  amostragem estatística, não uma contagem exata — registrar o tamanho da
  amostra usado junto ao resultado.

Infraestrutura: reaproveita `infra/envs/seed` (a mesma raiz enxuta de
`infra/scripts/seed_dataset_snapshots.py`) — Postgres/Scylla/OpenSearch
restauram do snapshot de dataset já existente (rápido, sem recarregar);
Valkey (sem disco persistente) paga uma carga completa, mesmo custo que já
acontece hoje em toda célula real dessa tecnologia. 4 execuções no total
(uma por tecnologia), não 14.

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
| serviço | `n2-standard-8` | 8 / 32 GB |
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
