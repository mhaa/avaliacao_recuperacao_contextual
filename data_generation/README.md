# Gerador de massa de dados — Fase 1 do TCC

Este diretório contém a implementação do gerador de massa de dados usado nos
experimentos, composto de um pacote Python (em `data_generation/generator/`)
que roda uma vez, offline, e produz os arquivos carregados nos quatro bancos e a suíte de testes que verifica os critérios de aceitação (em `data_generation/tests/`).

---

## Entrada

### Como executar

Tudo roda em container, via o serviço `generator` de `docker-compose.yml`
(raiz do repositório) — isola o gerador do Python do host, sem depender de
venv/conda locais. Da raiz do repositório:

```
docker compose build generator
docker compose run --rm generator all --sample-users 10000 --seed 42
```

roda a massa completa em escala de desenvolvimento (10.000 usuários) — é o
comando recomendado para iterar, já que a base real (~200 mil usuários) é
lenta para reprocessar a cada mudança. Para a escala real, basta omitir
`--sample-users`. Cada etapa também roda isolada, útil para depurar ou
reprocessar só uma parte sem refazer o resto:

```
docker compose run --rm generator ingest
docker compose run --rm generator rank
docker compose run --rm generator contexts
docker compose run --rm generator artifacts
docker compose run --rm generator oracle
docker compose run --rm generator synthetic --scale 1000000
```

`data_generation/data/` é montado como volume — os artefatos persistem no
host entre execuções. Para rodar a suíte de testes dentro do container:

```
docker compose run --rm --entrypoint python generator -m pytest tests -m "not integration"
```

(`python -m pytest`, não o script `pytest` direto — `python -m` insere o
diretório atual no `sys.path`, necessário para que `tests` seja importável
como pacote por `test_acceptance_criteria.py`.)

Toda etapa verifica se a sua saída já existe **com os mesmos parâmetros**
antes de rodar de novo — reexecutar sem `--force` é seguro e barato.
`--force` ignora essa checagem e força regeneração. `--seed` (default 42)
controla toda a aleatoriedade do processo; `--data-dir` sobrescreve onde os
artefatos são lidos/escritos (default `data_generation/data`, usado
principalmente pelos testes).

O download do MovieLens 32M é automático: a etapa `ingest` (ou `all`) baixa
e extrai o zip de `https://files.grouplens.org/datasets/movielens/ml-32m.zip`
na primeira execução, deixando os CSVs em `data/raw/ml-32m/`. Execuções
seguintes pulam o download se os arquivos já estiverem lá.

Os testes marcados `integration` exercitam os critérios de aceitação contra
o MovieLens 32M de verdade e exigem o dataset já baixado.

### Arquivos usados

| Arquivo | Conteúdo | Uso |
|---|---|---|
| `ratings.csv` | userId, movieId, rating, timestamp | matriz de interações para o ranking |
| `movies.csv` | movieId, title, genres | gêneros → contextos (combinações de 1-2, ver Etapa 3) |

O ml-32m real **não inclui `genome-scores.csv`/`genome-tags.csv`** — o tag
genome existiu no ml-20m/ml-25m e foi descontinuado no ml-32m — nem
`tags.csv` é usado (texto livre, sem score de relevância, não dá pra
substituir o genome de forma confiável). Isso só foi descoberto ao rodar
contra o dataset real pela primeira vez (o zip baixado não tinha esses
arquivos, quebrando a etapa `ingest`); a Etapa 3 foi redesenhada para
depender só de `movies.csv`. Ver "Decisões de implementação".

Escala nominal do MovieLens 32M: ~200.948 usuários, ~87.585 filmes, ~32M
avaliações. Confirmado batendo com a base real baixada (`raw_ratings_rows`
= 32.000.204, `raw_movies_rows` = 87.585 em `data/stats.json["ingest"]`, numa
execução com `--sample-users 10000`); os números efetivos de cada execução
sempre ficam registrados ali, sem depender dos valores nominais.

---

## Saída

Todos os artefatos ficam em `data/`, formato Parquet (compressão zstd),
exceto onde indicado.

### `id_maps.parquet` — tradução de IDs

Colunas: `entity` (`user`/`item`), `original_id`, `dense_id`. Existe porque
os `movieId`/`userId` do MovieLens são esparsos, e mapas de bits e listas
invertidas dependem de IDs densos (0..I-1, 0..U-1) para não desperdiçar
espaço. Produzido por `generator/ingest.py`; o universo de itens sempre
cobre todo `movies.csv` (independe de `--sample-users`), só o universo de
usuários é restrito quando a flag é usada.

### `candidates.parquet/` — conjunto de candidatos por usuário

Diretório de blocos (`block_0000.parquet`, `block_0001.parquet`, ...), cada
um cobrindo uma faixa contígua de até 100 mil usuários — a partição existe
para permitir carga incremental e para que a geração processe a base em
blocos, sem carregar as ~100M linhas inteiras em memória de uma vez.
Colunas: `user_id`, `item_id`, `rank` (1..500), `score`. Produzido por
`generator/rank.py`, via ALS (`implicit`, fatores=64, regularização=0.01,
iterações=15 — fixos, não são variáveis do estudo) sobre confiança implícita
`1 + 40 * rating`. Usuários com menos de 500 recomendações após excluir itens
já avaliados são completados com os itens mais populares restantes
(popularidade medida sobre todo o catálogo, não só os itens avaliados —
necessário para o preenchimento nunca travar em catálogos pequenos/esparsos).

### `items.parquet` — catálogo e atributos

Colunas: `item_id`, `original_movie_id`, `genres` (`list[int16]`, vocabulário
completo de gêneros do item — não só os gêneros usados nos 20 contextos
materializados). Produzido por `generator/contexts.py`.

### `contexts.parquet` — os 20 contextos materializados e sua seletividade

Colunas: `context_id` (0..19), `genre_ids` (`list[int16]`, 1 ou 2 gêneros —
a interseção AND que define o contexto), `label` (nomes concatenados com
`+`, ex. `"Comedy+Romance"`), `catalog_fraction`, `candidate_selectivity`,
`tier` (`high`/`medium`/`low`/`unused`). Contém exatamente os C=20 contextos
escolhidos para materialização (E-3/E-4), não todas as combinações
candidatas avaliadas (tipicamente ~190: os ~19 gêneros do MovieLens mais
seus pares que de fato coocorrem em algum filme). Três contextos carregam
`tier` high/medium/low, escolhidos por proximidade da `candidate_selectivity`
medida aos alvos (~2%/~20%/~60%, tolerância 5 p.p.); os demais preenchem até
20 pelas combinações mais frequentes (`catalog_fraction` desc). `candidate_selectivity`
é medida sobre uma amostra de usuários (mínimo 10 mil, ou todo o universo se
for menor) como a fração *média por usuário* dos candidatos que satisfaz o
contexto — deliberadamente diferente de `catalog_fraction`, porque os
candidatos são enviesados para itens populares. Produzido por
`generator/contexts.py`.

**Achado empírico rodando contra a base real (10.000 usuários):** os
patamares `high` (1,98%, alvo 2%) e `medium` (18,3%, alvo 20%) ficaram bem
dentro da tolerância de 5 p.p. O patamar `low` não — a combinação mais
próxima do alvo de 60% foi o gênero `Drama` sozinho, com 46,3% de
seletividade medida (desvio de ~13,7 p.p.). Isso é estrutural, não um bug:
uma interseção de dois gêneros nunca é mais frequente que o menor dos dois,
então nenhuma combinação de tamanho ≤2 consegue superar o gênero mais comum
do catálogo — e no MovieLens real, nem o gênero mais comum sozinho chega a
60%. Fica registrado em `stats.json["contexts"]["tiers"]["low"]["deviation_pp"]`
como resultado, coerente com a diretriz do projeto de documentar limitações
em vez de forçar um número.

### `inverted_lists.parquet` + `inverted_bitmaps/{context_id}.bin` — listas invertidas para E-4

`inverted_lists.parquet`: `context_id`, `item_ids` (`list[int32]`, ordenado
ascendente). Os `.bin` são a mesma informação serializada como bitmap Roaring
(`pyroaring`), um arquivo por contexto, para carga direta nas implementações
que fazem interseção de conjuntos. Produzido por `generator/artifacts.py`.

### `prematerialized.parquet` — pré-materialização para E-3

Uma linha por par (usuário, contexto) — **exatamente U × C linhas**, mesmo
quando não há nenhum item correspondente (nesse caso as listas ficam
vazias). Colunas: `user_id`, `context_id`, `item_ids` (top-40 do usuário
naquele contexto, ordenados por rank), `scores`. M=40 (não 20) porque a
exclusão de sessão é aplicada em tempo de requisição sobre o resultado lido;
armazenar só 20 produziria respostas incompletas depois de excluir itens já
vistos. Produzido por `generator/artifacts.py`.

### `oracle.parquet` — verificação de equivalência funcional

Colunas: `case_id`, `user_id`, `context_ids`, `exclude_ids`, `k`,
`expected_item_ids`, `expected_scores`. 1000 casos, cobrindo os três
patamares de seletividade, usuários de alta/baixa frequência (proxy: volume
de avaliações — não há log de acesso disponível na geração), contexto único
e composto (interseção AND de dois contextos), e três modos de exclusão
(vazia, parcial de 20 itens, ou pesada o bastante para forçar resposta
incompleta — pelo menos 50 casos acabam com menos de 20 itens esperados). O
resultado esperado é computado por uma implementação de referência ingênua,
direta sobre `candidates.parquet` em memória — **nunca** lê
`inverted_lists.parquet` nem `prematerialized.parquet`, porque esses são os
próprios artefatos de E-3/E-4 que serão testados contra o oráculo; usá-los
como fonte da verdade invalidaria o propósito do arnês. Produzido por
`generator/oracle.py`.

### `stats.json` — estatísticas descritivas

Uma seção por etapa (`ingest`, `rank`, `contexts`, `artifacts`, `oracle`,
`synthetic`), com os números que o TCC precisa citar: contagens reais,
parâmetros efetivos do ALS, seletividades medidas e seu desvio do alvo,
distribuição de preenchimento da pré-materialização, tamanhos de amostra do
oráculo. Deliberadamente **sem timestamps ou
durações** — é um dos arquivos comparados byte a byte no teste de
reprodutibilidade, e tempo de execução variaria entre corridas mesmo com o
mesmo resultado.

---

## Etapas de processamento

### Etapa 1 — Ingestão e densificação (`generator/ingest.py`)

Mapeia os `movieId`/`userId` esparsos do MovieLens para IDs densos
0..I-1/0..U-1, atribuídos por ID original ordenado. `--sample-users`
restringe só o universo de usuários — o de itens nunca muda, para que
`item_id` tenha o mesmo significado em qualquer execução.

### Etapa 2 — Ranking offline via ALS (`generator/rank.py`)

Fatoração de matrizes por mínimos quadrados alternados (Hu et al., 2008),
biblioteca `implicit`. Confiança implícita `1 + 40 * rating`. Fatores=64,
regularização=0,01, iterações=15, `random_state` derivado da seed —
parâmetros fixos, não ajustados para qualidade de recomendação, já que não
são variável do estudo. Recupera o top-500 por usuário excluindo itens já
avaliados; quem sobra abaixo de 500 é completado com os itens mais
populares restantes. Treino e inferência do ALS rodam com BLAS fixado em
uma thread (`threadpoolctl`) — sem isso, a redução paralela de ponto
flutuante quebra a reprodutibilidade byte a byte entre execuções.

### Etapa 3 — Construção dos contextos (`generator/contexts.py`)

Contextos são combinações de 1 ou 2 gêneros (interseção AND) — não
gênero+etiqueta como uma versão anterior desta spec previa (ver "Decisões
de implementação"). Gêneros vêm de `movies.csv` (split por `|`, descartando
`(no genres listed)`). Pares são calculados via self-join da tabela de
pertença item↔gênero (item_id em comum, genre_id_a < genre_id_b) — dá,
numa única operação vetorizada, exatamente os pares que de fato coocorrem
em algum filme, sem precisar enumerar e filtrar as C(19,2) combinações
teóricas uma a uma. A seletividade de cada combinação candidata é medida
com um único join+group by vetorizado sobre a amostra de usuários. Os três
patamares são escolhidos por argmin guloso da distância à
seletividade-alvo (com `combo_key` como critério de desempate — a distância
pode empatar entre combinações diferentes, e sem desempate determinístico
isso quebrava a reprodutibilidade); os 20 contextos materializados
completam com as combinações mais frequentes (`catalog_fraction` desc,
mesmo desempate).

### Etapa 4 — Artefatos derivados (`generator/artifacts.py`)

Constrói as listas invertidas (só para os 20 contextos materializados) e as
serializa também como bitmaps Roaring. A pré-materialização é montada como
um cross join usuário × contexto com left join dos resultados truncados em
top-40, para garantir exatamente U×C linhas mesmo nos pares sem nenhum item
correspondente.

### Etapa 5 — Oráculo (`generator/oracle.py`)

Gera os 1000 casos e computa o resultado esperado de forma independente
(filtragem ingênua em memória, nunca via os artefatos de E-3/E-4). A
ordenação — `rank` crescente, ou seja `score` decrescente com desempate por
`item_id` crescente — é a mesma usada no ranking da Etapa 2, para que
qualquer implementação testada tenha uma referência inequívoca.

---

## Expansão sintética (`generator/synthetic.py`)

Implementada, mas **ainda não executada** contra a base real — aplica-se só
à varredura de escalabilidade, não às três fases do experimento principal,
que rodam sobre a base real. Cada usuário sintético parte de um perfil real
amostrado com reposição; uma fração `p` (0,3 por padrão) dos seus 500
candidatos é substituída por itens sorteados pela distribuição empírica de
popularidade do catálogo, preservando a ordem relativa dos candidatos
remanescentes e inserindo os novos em posições sorteadas. Por escala, só os
artefatos dependentes de usuário são regenerados (`candidates`, `contexts`,
`prematerialized`) — `items.parquet` e as listas invertidas/bitmaps são de
catálogo e não mudam com o número de usuários.

A escala de validação obrigatória (mesma dimensão da base real, para testar
a fidelidade do gerador comparando distribuições real vs. sintética) usa o
`U` efetivamente medido em `stats.json["ingest"]`, nunca um número fixo —
coerente com a regra de nunca presumir os números do dataset.

---

## Requisitos transversais

**Sementes.** Toda fonte de aleatoriedade recebe uma seed derivada da seed
mestre via `numpy.random.SeedSequence`, registrada em `stats.json`.

**Reexecução idêntica.** Verificado automaticamente
(`tests/test_acceptance_criteria.py::test_rerun_with_same_seed_is_byte_identical`,
rodando `all` duas vezes em processos separados e comparando cada artefato
byte a byte). Três fontes de não-determinismo foram encontradas e
corrigidas nesse processo: agregações float do polars (`mean`, `sum`) não
são bit-reprodutíveis sob paralelismo — corrigido fixando
`POLARS_MAX_THREADS=1` em `generator/__main__.py`, antes de qualquer import
que toque polars; `group_by()` não garante a ordem das linhas dentro de
cada grupo, o que quebrava a reprodutibilidade em pontos onde essa ordem
alimentava `rng.choice` (seleção de usuários e exclusões do oráculo) —
corrigido com `.sort()`/`maintain_order=True` explícitos nesses pontos; e a
seleção dos patamares de contexto ordenava por distância/frequência sem
desempate determinístico — quando duas combinações de gênero empatavam
(comum com poucos contextos candidatos), qual delas "vencia" dependia de
ordem de linha não garantida — corrigido acrescentando `combo_key` como
critério de desempate nesses `.sort()`.

**Memória.** `candidates.parquet` é escrito em blocos de até 100 mil
usuários via `polars` em modo lazy, sem carregar a base inteira de uma vez.

**Idempotência.** Cada etapa compara os parâmetros da execução anterior
(registrados em `stats.json`) contra os da execução atual antes de decidir
se pula ou refaz — não só se os arquivos de saída existem.

---

## Critérios de aceitação

Verificados pela suíte de testes (`data_generation/tests/`), em escala de
fixture fabricada (rápida, sem rede). `all --sample-users 10000 --seed 42`
também já rodou manualmente contra o MovieLens 32M real (via
`docker compose run --rm generator all --sample-users 10000 --seed 42`),
confirmando os critérios 1, 2, 3, 4, 6, 9 na prática; o teste formal
`@pytest.mark.integration` que automatiza essa verificação ainda não foi
executado nesta máquina.

| # | Critério | Teste | Status na base real |
|---|---|---|---|
| 1 | `all --sample-users 10000` conclui sem erro | `test_acceptance_criteria.py::test_full_pipeline_runs_without_error` | ✅ confirmado |
| 2 | Reexecução com mesma seed produz arquivos idênticos | `test_acceptance_criteria.py::test_rerun_with_same_seed_is_byte_identical` | ✅ confirmado (rerun idempotente) |
| 3 | `rank` contíguo 1..500, sem lacunas/duplicatas por usuário | `test_rank.py` | ✅ confirmado |
| 4 | `item_id` dentro de 0..I-1 | `test_rank.py` | ✅ confirmado |
| 5 | Seletividades dos três patamares a até 5 p.p. do alvo | `test_contexts.py` (unit) | ⚠️ `high`/`medium` dentro da tolerância; `low` desvia ~13,7 p.p. — ver nota em "Saída" |
| 6 | `prematerialized.parquet` com exatamente U×C linhas | `test_artifacts.py` | ✅ confirmado |
| 7 | Bitmap Roaring ∩ == filtro ingênuo, em ≥100 casos | `test_artifacts.py` | fixture apenas |
| 8 | 1000 casos do oráculo, todos com resultado coerente | `test_oracle.py` | ✅ confirmado (1000 casos, 649 com <20 itens) |
| 9 | `stats.json` com todos os números citáveis pelo TCC | `test_acceptance_criteria.py::test_stats_json_has_all_required_numbers` | ✅ confirmado |

---

## Decisões de implementação

Pontos que a especificação original deixava em aberto (ou que a realidade do
dataset invalidou) e precisaram de uma escolha concreta:

- **Contextos via combinação de 1-2 gêneros, não gênero+etiqueta.** A
  versão original desta spec assumia tag genome (`genome-scores.csv`/
  `genome-tags.csv`) para os contextos de alta restrição, com `tags.csv`
  como fallback. Isso quebrou ao rodar contra o dataset real: o ml-32m não
  inclui genome (só ml-20m/ml-25m tinham), e `tags.csv` é texto livre sem
  score de relevância — não dá pra reconstruir um vocabulário de etiquetas
  confiável a partir dele. A alternativa adotada — combinações AND de 1-2
  gêneros — usa só `movies.csv`, que sempre existe, e dá granularidade de
  seletividade suficiente sem depender de dado que pode não estar presente.
- Fórmula de confiança do ALS: `1 + 40 * rating` (convenção Hu et al.).
- Schema de `id_maps.parquet`: formato longo `(entity, original_id, dense_id)`.
- `candidates.parquet` é um diretório de blocos, não um arquivo único — leitura
  direta do requisito de particionamento em blocos de 100 mil usuários.
- `items.parquet` usa o vocabulário completo de gêneros do item, não só os
  gêneros usados nos 20 contextos materializados.
- `contexts.parquet` contém exatamente os 20 contextos selecionados, não
  todas as combinações candidatas avaliadas.
- A escala de validação sintética usa o `U` medido, não um número fixo.
- Por escala sintética, só os artefatos dependentes de usuário são regenerados.
- "Frequência de acesso" no oráculo é aproximada por volume de avaliações.
- Contextos compostos no oráculo (interseção AND de `context_ids` em tempo
  de requisição) são um conceito diferente e independente das combinações de
  gênero que definem cada `context_id` individualmente — um caso composto
  pode pedir `context_ids=[5, 12]`, cada um já sendo, por exemplo,
  `Action+Comedy` e `Drama`.
- `stats.json` não contém timestamps/durações.

---

## Fora de escopo

O ALS não foi ajustado para qualidade de recomendação — não é variável do
estudo. A massa sintética não roda antes de a base real estar validada. Os
números do dataset não são presumidos em lugar nenhum do código — são
medidos e registrados em `stats.json`. Identificadores originais do
MovieLens não aparecem nos artefatos finais, só em `id_maps.parquet`.
Nenhuma das quatro estratégias de recuperação (E-1..E-4) é implementada
aqui — este módulo produz dados; as estratégias ficam nos serviços que os
consomem.
