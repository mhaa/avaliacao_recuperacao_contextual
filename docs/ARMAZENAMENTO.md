# Volume de armazenamento por célula — resultados e análise

Análise dos volumes ocupados por cada célula (estratégia × tecnologia) na base
real completa (U = 200.948, I = 87.585, C = 20, N = 500), medidos uma única vez
por tecnologia contra a base já carregada, conforme o protocolo de
[DESIGN.md](DESIGN.md) ("Custo — por milhão de requisições" → "O que medir, por
estratégia e tecnologia").

**Fontes dos números.** Medição bruta em `results/storage/{postgres,valkey,scylla,opensearch}.json`,
produzida por `analysis/storage_size.py`. Tabelas consolidadas em
`results/report/extra/storage_by_cell.csv` (formato longo, uma linha por célula)
e `results/report/extra/storage_by_cell_wide.csv` (formato largo, uma linha por
estratégia). Contagens de linhas/chaves derivadas dos artefatos de
`data_generation/data/` e de `data_generation/data/stats.json`.

---

## Volumes medidos

| Estratégia | Postgres | Valkey | ScyllaDB | OpenSearch |
|---|---|---|---|---|
| E-1 | 10,28 GiB | 2,31 GiB | 396,6 MiB | 6,94 GiB |
| E-2 | 10,29 GiB | 2,31 GiB | 1,10 GiB | 6,94 GiB (= E-4) |
| E-3 | 14,87 GiB | 3,46 GiB | 545,9 MiB | inviável |
| E-4 | 10,28 GiB | 286,6 MiB | inviável | 6,94 GiB (= E-2) |
| *medição* | *exata* | *estimada* | *estimada* | *exata* |

### Estruturas subjacentes

| Artefato | Cardinalidade | Postgres | Valkey | ScyllaDB | OpenSearch |
|---|---|---|---|---|---|
| `candidates` | 100.474.000 (U×N) | 11.037.474.816 B | 2.477.492.715 B (200.948 chaves) | 415.842.447 B | 7.446.590.569 B |
| `item_contexts` | 167.074 pares / 79.602 itens | 12.492.800 B | 4.492.737 B | 15.942.400 B | 3.182.161 B (índice de catálogo) |
| `inverted_lists` | 20 listas / 167.074 refs | 811.008 B | 3.896.432 B (20 chaves) | — | — |
| `candidates_set` | 200.948 chaves × 500 | — | 296.631.400 B | — | — |
| `candidates_by_context` | 305.120.643 | — | — | 1.185.564.416 B | — |
| `prematerialized` | 138.640.226 linhas explodidas | 15.970.254.848 B | 3.717.056.253 B (3.995.082 chaves) | 572.421.246 B | inviável |

### Custo por unidade armazenada (tabela `candidates`, mesma informação lógica)

| Tecnologia | Custo por candidato |
|---|---|
| Postgres | ~109,9 bytes/linha |
| OpenSearch | ~74,1 bytes/documento |
| Valkey (HASH, E-1) | ~24,66 bytes/candidato |
| Valkey (SET/intset, E-4) | ~2,95 bytes/item |
| ScyllaDB | ~4,14 bytes/linha |

---

## Texto para o TCC

Os maiores volumes foram observados no PostgreSQL, cuja estrutura tabular
tradicional armazena, para cada registro, `user_id`, `item_id`, `rank` e
`score`. Na estratégia E-2 observou-se um aumento marginal (10,28 → 10,29 GiB,
+0,11%), decorrente da atribuição à célula da tabela de pertença item→contexto
(`item_contexts`, 12,5 MB): em E-1 esse catálogo é lido uma única vez na subida
do serviço e mantido em memória da aplicação, ao passo que em E-2 é consultado
por requisição via `JOIN`. Por ser dado de catálogo — dimensão O(itens), não
O(usuários × itens) — seu custo é desprezível frente à tabela de candidatos. Na
estratégia E-3 há aumento expressivo (14,87 GiB, +44,6%), decorrente da
pré-materialização por contexto: a tabela explode em uma linha por trio
(usuário, contexto, item), totalizando 138.640.226 registros contra os
100.474.000 da tabela de candidatos, já que se armazenam até 40 itens para cada
um dos 20 contextos por usuário (média efetiva de 34,5, pois nem todo par
usuário-contexto atinge o teto). A estratégia E-4 retorna ao patamar original
(10,28 GiB), pois a lista invertida global ocupa apenas 811 KB em 20 registros —
uma linha por contexto —, variação irrelevante no total.

O Valkey apresenta volume substancialmente inferior (2,31 GiB em E-1) por quatro
diferenças estruturais: não armazena o `rank`, reconstruído na aplicação a
partir do score; não repete o `user_id`, que compõe o nome da chave e é pago uma
vez por usuário e não uma vez por candidato; não mantém índice secundário, já
que a chave é o próprio caminho de acesso, enquanto o número do PostgreSQL
inclui o índice covering `idx_candidates_user_rank`, que duplica as colunas em
todas as linhas; e não paga overhead transacional por registro (cabeçalho de
tupla MVCC, ponteiro de linha e padding de alinhamento), custo fixo por linha no
PostgreSQL. Na estratégia E-4 a redução é ainda mais acentuada (286,6 MiB), por
dois efeitos combinados: a estrutura `candidates_set` guarda apenas o `item_id`,
sem o score, e — sobretudo — um conjunto composto exclusivamente de inteiros com
cardinalidade inferior ao limiar `set-max-intset-entries` (512 por padrão) é
codificado internamente como `intset`, um vetor ordenado de largura fixa, ao
passo que a estrutura de E-1, com 500 campos, excede o limiar
`hash-max-listpack-entries` (128) e é forçada à codificação `hashtable`, que
paga overhead de dicionário por campo. O custo por elemento cai de ~24,66 para
~2,95 bytes, cerca de 8,4× — muito além do que a ausência do score explicaria
isoladamente. Registre-se que N=500 situa-se a apenas doze unidades do limiar de
512, de modo que uma escolha marginalmente diferente do parâmetro alteraria a
codificação e, com ela, o volume observado.

O OpenSearch situa-se em posição intermediária (6,94 GiB): tampouco armazena o
`rank`, mas, ao contrário do Valkey, mantém três representações simultâneas do
mesmo dado — o documento original (`_source`), o índice invertido dos campos
indexados e os doc values colunares. Soma-se a isso a desnormalização de
`context_ids` em cada um dos 100.474.000 documentos e a repetição do `user_id`
por documento. Essa redundância não é desperdício: é o que permite resolver o
predicado de contexto nativamente dentro do índice, razão pela qual E-2 e E-4
compartilham o mesmo índice físico e são mecanicamente indistinguíveis nessa
tecnologia, enquanto E-3 é arquiteturalmente destituída de sentido e não foi
implementada.

O ScyllaDB apresenta o menor volume (396,6 MiB em E-1), eliminando igualmente o
índice secundário e o overhead transacional por linha, armazenando a chave de
partição (`user_id`) uma única vez por partição em vez de uma vez por linha, e
aplicando compressão LZ4 às SSTables por padrão. Um indício da consistência
dessa leitura é que suas três tabelas convergem para ~4 bytes por linha apesar
de cardinalidades muito distintas: uma vez descontada a chave de partição, todas
compartilham exatamente o mesmo payload de clustering (`rank`, `item_id`,
`score`). A estratégia E-2 constitui o caso extremo do experimento: na ausência
de `JOIN` em CQL, a filtragem por contexto exige materializar previamente o
produto entre candidatos e pertença de contexto, resultando em 305.120.643
registros — a maior estrutura de qualquer célula — ainda assim contidos em
1,10 GiB. A estratégia E-4 é inviável nessa tecnologia, por ausência de
primitiva nativa de interseção de conjuntos.

Cumpre ressalvar que as medições não possuem o mesmo grau de exatidão:
PostgreSQL e OpenSearch foram medidos diretamente (`pg_total_relation_size` e
`_cat/indices`), ao passo que os valores de Valkey e ScyllaDB são estimativas,
obtidas respectivamente por amostragem de `MEMORY USAGE` e pela tabela virtual
`system.size_estimates`. A comparação mais expressiva — ScyllaDB
aproximadamente 26 vezes menor que PostgreSQL — apoia-se justamente na medição
menos exata, o que recomenda cautela quanto à magnitude, ainda que a direção do
resultado seja estruturalmente esperada.

---

## Fundamentação das afirmações acima

Cada afirmação do texto, com a referência no código que a sustenta — necessário
para defender os números sem reabrir a investigação.

**O `rank` é recomputado na aplicação em todas as células.**
`core/ordering.py:16` ordena por `(-score, item_id)` e atribui o rank na
resposta. Não é uma particularidade do Valkey: a HASH `candidates:{user_id}` não
tem ordem alguma (`HGETALL` devolve campos em ordem arbitrária), e o rank é
derivado do score, não de um armazenamento ordenado. O PostgreSQL persiste a
coluna `rank` mesmo assim, porque ela é a chave de ordenação do índice covering
`idx_candidates_user_rank` (`schemas/postgres/001_schema.sql:24-25`).

**`item_contexts` existe fisicamente em todas as células.**
`schemas/*/load_full_dataset.py` carrega as quatro estruturas de uma vez, sem
depender de estratégia. O que muda em E-2 é a **atribuição**: E-1 usa o catálogo
em memória da aplicação (`core/catalog.py`), então um deploy que servisse apenas
E-1 não precisaria da tabela — critério declarado em
[DESIGN.md](DESIGN.md) ("Catálogo item→contexto residente na aplicação").

**A pré-materialização é explodida, não armazenada como array.**
`schemas/postgres/002_prematerialized.sql` define
`PRIMARY KEY (user_id, context_id, item_id)` — uma linha por item.
`harness/fixtures.py:load_prematerialized` faz o `explode` das listas do
artefato bruto. Por isso a tabela tem 138.640.226 linhas, e não as 4.018.960
(U×C) do `prematerialized.parquet`, que guarda uma lista por par.

**Codificação `intset` no Valkey.**
Nenhum arquivo do projeto sobrescreve `set-max-intset-entries` nem
`hash-max-listpack-entries` (verificado em `docker-compose.yml` e `infra/`) —
valem os padrões 512 e 128. `candidates_set:{user_id}` tem exatamente 500
membros inteiros (abaixo de 512 → `intset`); `candidates:{user_id}` tem 500
campos (acima de 128 → `hashtable`).

**Três representações no Lucene.**
`schemas/opensearch/create_index.py` não desabilita `_source`, e
`storage/opensearch.py:118` o lê de volta — logo o documento original é
armazenado além das estruturas de índice. O mapping declara `user_id`,
`item_id`, `score` e `context_ids` (sem `rank`), e
`schemas/opensearch/load_full_dataset.py:84-91` desnormaliza `context_ids` em
cada documento.

**Compressão e layout do ScyllaDB.**
`schemas/scylla/apply_schema.py` define apenas `compaction`
(LeveledCompactionStrategy), sem desabilitar compressão — vale o padrão LZ4. As
três tabelas têm chaves de partição distintas (`user_id`;
`(context_id, user_id)`; `(user_id, context_id)`) mas payload de clustering
idêntico (`rank`, `item_id`, `score`), o que explica a convergência para ~4
bytes/linha.

**Contagem de chaves da pré-materialização no Valkey.**
3.995.082 chaves e não 4.018.960 (U×C): os 23.878 pares usuário-contexto sem
nenhum item correspondente não geram chave. O número bate exatamente com
`stats.json["artifacts"]["prematerialized_fill_distribution"]["0"]`.

---

## Ressalvas de medição

- **Exatas**: PostgreSQL (`pg_total_relation_size`, inclui heap + índices +
  TOAST) e OpenSearch (`_cat/indices?bytes=b`, store size em disco).
- **Estimada por amostragem**: Valkey — `SCAN` conta as chaves de verdade
  (exato), e até 2.000 delas são amostradas via `MEMORY USAGE`
  (`analysis/storage_size.py:134-168`).
- **Estimada pelo próprio banco**: ScyllaDB — `system.size_estimates`
  (`mean_partition_size × partitions_count` por faixa de token), não uma leitura
  de bytes em disco.
- Para elevar a confiança na comparação do ScyllaDB (a afirmação mais forte,
  ~26× menor que o PostgreSQL), convém confirmar a ordem de grandeza com
  `nodetool tablestats` ou o tamanho real das SSTables em disco numa próxima
  execução em nuvem.
- Não há decomposição heap vs. índice para o PostgreSQL: `pg_total_relation_size`
  devolve a soma. Isolar quanto do 10,28 GiB é o índice covering exigiria
  `pg_relation_size` e `pg_indexes_size` separados, contra uma base carregada.
