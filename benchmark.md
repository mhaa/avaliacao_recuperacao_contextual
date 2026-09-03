# Expectativa de latência — análise anterior à medição

**Status: estimativas de engenharia, não medições** — com uma exceção, a seção
9, que traz o único número medido de verdade até aqui (tempo de montagem do
catálogo). Nenhuma latência de requisição foi medida. O objetivo é ter uma
régua *antes* da primeira bateria válida, para distinguir "resultado legítimo
da tecnologia" de "erro de configuração" — sem essa régua, um p99 de 800 ms
parece tão plausível quanto um de 8 ms.

Escrito em 2026-09-02, revisado em 2026-09-03 após a mudança do catálogo
item→contexto (README, "Fase 2.6"), que alterou o custo de E-1 nas quatro
tecnologias. `results/` estava vazio nas duas datas.

Substituir as colunas de estimativa por medição real assim que a triagem
produzir dados válidos, mantendo o previsto ao lado do medido — a diferença
entre os dois é, por si, material de discussão do TCC.

---

## 1. A referência dos 200 ms não é o que parece

O número do Pinterest usado no texto do TCC é o **orçamento da pipeline
inteira** — retrieval + ranking + blending + rede até o cliente. O dado
diretamente comparável está no mesmo material: o **estágio de retrieval
sozinho** custava 200 ms numa implementação que buscava metadados completos de
todos os candidatos, e caiu para **75 ms** ao reduzir o metadado em 3×.

Essa é exatamente a decisão de projeto que o contrato da API já tomou
("resposta sem metadados descritivos, ~800 bytes"). A referência honesta para o
que esta bancada mede, portanto, é a casa dos **75 ms — e para uma pipeline que
ainda tem ranking por cima, que aqui não existe.** O teto de 300 ms é folgado
por uma ordem de grandeza.

Não confundir com o SLO do protocolo de medição: os **200 ms de p99 são o
gatilho de saturação**, o ponto em que a célula é declarada estourada na rampa.
Não são latência esperada. Sob carga fixa, as células deveriam ficar bem abaixo
disso; uma célula medindo perto de 200 ms a 1.000 req/s é sinal de problema,
não de sucesso.

## 2. O volume aqui é pequeno para padrões de benchmark

U = 200.948 × N = 500 ≈ **100M linhas de candidatos**, 87.585 itens, C = 20
contextos. Contra a VM de banco (`n2-standard-8`, 8 vCPU / 32 GB):

| Tecnologia | Volume estimado | Cabe em RAM? |
|---|---|---|
| Valkey | ~0,8–10 GB (conforme a estrutura) | sim (`maxmemory 24gb`) |
| ScyllaDB | ~2–5 GB, 200k partições de ~5–10 KB | sim (`--memory 28G`) |
| PostgreSQL | ~5–8 GB heap + índice | sim (page cache de 32 GB) |
| OpenSearch | ordem de GB | sim (heap 16g + page cache) |

**Depois do aquecimento, nenhuma célula deveria tocar disco.** Isso é o
argumento central da comparação com a literatura: os benchmarks publicados
abaixo rodam com volumes de 10× a 1000× maiores. Ficar *mais lento* que um
benchmark que processa 40 TB é sintoma de configuração, não de física.

Ressalva: `pd-ssd` de 200 GB no GCP dá ~6.000 IOPS de leitura e ~96 MB/s —
fraquíssimo perto dos NVMe locais dos benchmarks da ScyllaDB. Só não importa
porque nada deveria ir ao disco. Se alguma célula **degradar ao longo do
teste**, o disco é o primeiro suspeito.

## 3. Benchmarks públicos de referência

| Tecnologia | p99 publicado | Condições | Fonte |
|---|---|---|---|
| Valkey | **0,8 ms** a ~1M ops/s | c8g.2xlarge, SET, memtier | [valkey.io](https://valkey.io/blog/what-is-valkey-benchmark/), [cache-benchmarks](https://github.com/tidwall/cache-benchmarks) |
| ScyllaDB | **< 10 ms** a 160–180k ops/s; **< 10 ms** a 500k ops/s | 3× i3.4xlarge (48 vCPU) / 4× i3.metal, 40 TB, RF=3 | [scylladb.com](https://www.scylladb.com/2021/08/24/scylladb-vs-cassandra-performance-results/) |
| ScyllaDB | 5,7–8,7 ms (leitura) | 16 instâncias YCSB, 200 threads cada, alvo 500k ops/s | [BenchANT](https://benchant.com/blog/mongodb-vs-scylladb-benchmark) |
| Elasticsearch | term query **2,46 ms** (p90) local; ~20 ms (p90) nos gráficos da Elastic | 1,74 bilhão de docs, Ryzen 5600X + NVMe | [blunders.io](https://blunders.io/posts/es-benchmark-3-latency) |
| OpenSearch | nightly em **c5.2xlarge single-node, 116M docs / ~100 GB** (8 vCPU / 16 GB, heap 8 GB) | Big5 / PMC | [opensearch.org](https://opensearch.org/blog/opensearch-project-update-performance-progress-in-opensearch-3-0/) |
| PostgreSQL | index scan para localizar uma linha: **~0,016 ms** | — | [Cybertec](https://www.cybertec-postgresql.com/en/postgresql-performance-latency-in-the-cloud-and-on-premise/) |

O setup nightly do OpenSearch (c5.2xlarge, 8 vCPU, 116M documentos) é **quase
idêntico ao desta bancada** em hardware e volume — é a comparação mais direta
disponível.

Dois avisos sobre os números de Scylla: (a) esses p99 de 5–10 ms são sob carga
extrema (500k ops/s); a 1.000 req/s Scylla opera na casa de sub-ms a 1–2 ms;
(b) i3.metal é bare metal com NVMe local, enquanto `n2-standard-8` são 8 *vCPUs*
(4 cores físicos + hyperthreading) com disco de rede. `--smp 7` sobre
hyperthreads não replica o comportamento shard-per-core dos benchmarks
oficiais. Esperar pior que o publicado, por um fator pequeno.

## 4. O orçamento real de uma n2-standard-8

CPU de banco disponível por requisição, para manter utilização em ~30% (acima
disso a cauda explode por enfileiramento, em qualquer tecnologia):

| Carga | CPU/req a 100% | **Orçamento realista (30%)** |
|---|---|---|
| 100 req/s | 80 ms | ~24 ms |
| 1.000 req/s | 8 ms | **~2,4 ms** |
| 10.000 req/s | 0,8 ms | **~240 µs** |

**Para o Valkey o orçamento é 1 CPU-segundo/s, não 8** — a execução de comandos
é single-thread. A 10.000 req/s isso são **100 µs por requisição**, e nenhuma
vCPU extra ajuda.

## 5. Custo por requisição, célula a célula

Estado do código após a mudança do catálogo (README, "Fase 2.6"). Ordens de
grandeza, não previsões precisas.

| Célula | Trabalho por requisição | CPU/req est. | Teto est. (30%) |
|---|---|---|---|
| E-3/Valkey | 1 `HGETALL` de ~40 campos | ~15–30 µs | ~10–20k req/s |
| E-3/Scylla | 1 partição pequena, cacheada | ~50–150 µs | ~15–45k req/s |
| E-1/Valkey | 1 `HGETALL` de 500 campos | ~50–100 µs | ~3–6k req/s |
| E-1/Scylla | 1 partição de 500 linhas | ~100–300 µs | ~8–24k req/s |
| E-3/Postgres | index scan, ~40 linhas | ~0,15–0,4 ms | ~6–15k req/s |
| E-4/Valkey | `SINTER`(500 × lista invertida) + `HMGET` | ~0,1–0,3 ms | ~1–3k req/s |
| E-1/Postgres | index-only scan, 500 linhas | ~0,3–0,8 ms | ~3–8k req/s |
| E-2/Scylla | 1 partição por contexto + interseção em Python | ~0,1–0,3 ms × \|ctx\| | ~5–15k req/s |
| E-2/Postgres | `JOIN` + `GROUP BY` + `HAVING COUNT(DISTINCT)` | ~1–3 ms | ~0,8–2,4k req/s |
| E-2/Valkey | Lua: `HGETALL` + até 500×\|ctx\| `SISMEMBER` | ~1–3 ms *single-thread* | **~0,1–0,3k req/s** |
| E-1, E-2, E-4/OpenSearch | `bool filter`, `size=500`, fetch de 500 `_source` | ~3–15 ms | **~0,15–0,8k req/s** |

Sobre o **E-2/Valkey**: o script Lua percorre os 500 candidatos com um
`SISMEMBER` por item e por contexto, dentro da thread única. É caro, mas é
*legítimo* — E-2 significa "o predicado é avaliado dentro do banco", e é
exatamente isso que está acontecendo. Diferente do E-1 antigo, onde o custo
equivalente vinha de uma escolha de modelagem do adaptador e não da estratégia.
Se essa célula saturar cedo, é resultado, não bug.

## 6. O piso da bancada

O caminho é k6 → Hypercorn/FastAPI (`n2-standard-4`, 4 workers) → banco →
volta. Um POST FastAPI com validação Pydantic de entrada **e de saída**
(`response_model=Response` revalida cada item) custa alguns ms de p99 sozinho.
Rede intra-VPC: ~0,2–0,5 ms por hop, desprezível.

**Piso estimado: ~3–8 ms de p99.** Nenhuma célula desce disso, por melhor que
seja o banco. Consequências:

- Em E-3, mede-se majoritariamente o overhead do FastAPI, não a tecnologia.
  Isso comprime a diferença entre Valkey e Scylla e **é uma ameaça à validade
  que precisa estar no texto do TCC**.
- A VM de serviço (`n2-standard-4`) é frequentemente o gargalo real, não o
  banco. Já houve precedente: o serviço ficou preso em ~28% de CPU com
  latências de 10–24 s até o número de workers ser corrigido.

**Como medir:** `POST /v1/baseline` (service/http_app.py) devolve k itens
sintéticos sem tocar o banco. Percorre exatamente o mesmo caminho de
`/v1/recommendations` — mesmo parse de `context`/`exclude`, mesma construção
de `ResponseItem`, mesma revalidação pelo `response_model`, mesma
serialização, mesmos dois hops — exceto a leitura e a filtragem. Existe em
toda célula, então o piso é medível **no mesmo deploy** que está sendo medido.

O k6 não precisa de cenário novo: `TARGET_URL` já vem de `__ENV`
(load/scenarios.js).

```
TARGET_URL=http://<ip-do-servico>:8000/v1/baseline \
CELL=<celula> K=20 RATE=1000 k6 run load/scenarios.js
```

A subtração "latência da célula − piso" só é legítima se os dois payloads
tiverem o mesmo tamanho, porque serialização e rede escalam com bytes. Isso é
garantido por teste (`test_baseline_payload_matches_real_response_size`,
tolerância de 5% contra uma resposta real de E-1 com ids e scores de largura
realista), não por inspeção — é por isso que os valores sintéticos usam ids de
5 dígitos e scores de 6 casas decimais, e não `1, 2, 3` com score `1.0`.

Medir o piso **antes** de interpretar qualquer diferença entre células. Ele
também é número publicável no TCC: quantifica quanto da latência observada é
da bancada e não da tecnologia — a ameaça à validade descrita acima.

## 7. Régua de diagnóstico

A 1.000 req/s, seletividade média:

| p99 observado | Leitura |
|---|---|
| < 50 ms | normal, coerente com a literatura |
| 50–150 ms | plausível só para OpenSearch; nas outras três, investigar |
| > 200 ms | quase certamente configuração ou saturação |
| segundos + erros | **não é latência, é fila** — ver abaixo |

**Antes do percentil, olhar `throughput_rps` vs. `rate` do manifest.** Em modelo
aberto, capacidade insuficiente vira explosão de latência. Exemplo real, dos
resultados arquivados em `resultados-invalidos-pre-correcao/e1-postgres/`
(rep0): p50 = 8.516 ms, p99 = 16.585 ms, 90% de erro e **215 req/s atendidos de
1.000 pedidos**. Nada ali diz respeito ao PostgreSQL.

Discriminantes por sintoma:

- **p99 ≫ 100× p50** → enfileiramento (concorrência, pool, workers), não custo
  por requisição.
- **latência sobe ao longo do teste** → cache que não aquece, pressão de
  memória, GC (OpenSearch) ou compaction (Scylla).
- **Postgres > 20 ms com tudo em RAM** → índice ausente/seq scan, ou pool
  estrangulando. Confirmar com `EXPLAIN (ANALYZE, BUFFERS)`: se aparecer
  `read=` em vez de só `hit=`, não aqueceu.
- **Scylla > 10 ms sob carga baixa** → `--smp`/`--memory`, ou reactor stalls.
  Já houve precedente: `--smp 1 --memory 2G` herdado do compose local.
- **Valkey > 5 ms** → o loop principal saturou, ou há comando O(N) grande no
  caminho.
- **OpenSearch > 150 ms** → shards demais para 1 nó, `refresh_interval` padrão
  de 1s, ou `terms` sem cache.

## 8. Recomendação de sequência

1. **Rodar a triagem a 100 req/s antes de 1.000.** É o único patamar em que
   nenhuma célula satura, logo o único em que os benchmarks publicados servem
   de régua direta para validar configuração.
2. **Medir o piso da bancada** (endpoint de baseline, seção 6) antes de
   interpretar qualquer diferença entre células.
3. Só então subir para 1.000 e 10.000 req/s, esperando que boa parte da matriz
   sature — o que é a terceira dimensão da fronteira de Pareto, não uma falha.

## 9. Medido: tempo de montagem do catálogo (`prepare`)

Único número real deste documento. Medido em 2026-09-03 no ambiente **local**
(Docker, fixture do oráculo), com a instrumentação de `core/catalog.py:
load_catalog`. Não é latência de requisição — `prepare` roda uma vez na
montagem da célula, fora do caminho medido.

| Storage | `prepare` | Itens | Técnica do despejo |
|---|---|---|---|
| Valkey | **0,265 s** | 79.602 | 1 `HGETALL` de `catalog:item_contexts` |
| PostgreSQL | **0,34 – 0,52 s** | 79.602 | `GROUP BY` + `array_agg`, 1 query |
| ScyllaDB | **0,50 s** | 79.602 | varredura paginada (`fetch_size=10.000`) |
| OpenSearch | **1,98 s** | 79.602 | `search_after`, ~9 páginas de 10.000 |

As quatro concordam em 79.602 itens — checagem cruzada de que nenhum despejo
está truncando. E 79.602 **já é o tamanho real** do catálogo: ele deriva de
`items.parquet` global, não do subconjunto de usuários do oráculo.

**Esses números devem se sustentar na base cheia.** A propriedade que importa é
que nenhuma das quatro escala com U — todas as estruturas de catálogo têm
tamanho fixo:

| Storage | Escala com | Na nuvem |
|---|---|---|
| Valkey | catálogo (~80 mil, fixo) | ~0,3 s |
| PostgreSQL | tabela `item_contexts` (fixa) | ~0,5 s |
| ScyllaDB | tabela `item_contexts` (fixa) | ~0,5 s |
| OpenSearch | índice `item_contexts` (~80 mil docs, fixo) | ~2 s |

Era exatamente essa propriedade que faltava ao Valkey: a primeira versão
enumerava as chaves `item_contexts:*` com `scan_iter`, e `SCAN` percorre o
keyspace **inteiro** filtrando no servidor — custo proporcional ao total de
chaves (~4,4 milhões na base cheia, contra ~102 mil localmente), não ao
catálogo. Medido em 0,849 s localmente, extrapolava para ~37 s por worker na
nuvem, com 4 workers concorrentes contra a thread única do Valkey. O hash de
catálogo dedicado eliminou a dependência.

**Efeito na subida do serviço:** `prepare` roda uma vez por worker Hypercorn,
4 em paralelo (`spawn`). Pior caso é o OpenSearch: entre ~2 s e ~8 s até o
serviço aceitar tráfego. Folgado para qualquer health check razoável, mas é
número conhecido em vez de suposição.

---

## Fontes

- Pinterest Engineering — [Establishing a Large Scale Learned Retrieval System](https://medium.com/pinterest-engineering/establishing-a-large-scale-learned-retrieval-system-at-pinterest-eb0eaf7b92c5)
- Valkey — [Simulating Real Workloads with valkey-benchmark](https://valkey.io/blog/what-is-valkey-benchmark/)
- tidwall — [cache-benchmarks](https://github.com/tidwall/cache-benchmarks)
- ScyllaDB — [ScyllaDB vs Cassandra Performance Benchmark](https://www.scylladb.com/2021/08/24/scylladb-vs-cassandra-performance-results/)
- BenchANT — [MongoDB vs ScyllaDB](https://benchant.com/blog/mongodb-vs-scylladb-benchmark)
- blunders.io — [Elasticsearch Benchmarking, Part 3: Latency](https://blunders.io/posts/es-benchmark-3-latency)
- OpenSearch — [Performance progress in OpenSearch 3.0](https://opensearch.org/blog/opensearch-project-update-performance-progress-in-opensearch-3-0/)
- Cybertec — [PostgreSQL Performance: Latency in the Cloud and On Premise](https://www.cybertec-postgresql.com/en/postgresql-performance-latency-in-the-cloud-and-on-premise/)
