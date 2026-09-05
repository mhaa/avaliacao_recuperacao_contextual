-- Lista invertida global por contexto, para E-4 (interseção de conjuntos) —
-- ver docs/DESIGN.md, matriz de viabilidade ("intarray/roaring"), e a
-- docstring de storage/postgres.py:intersect.
--
-- `intarray` é módulo contrib oficial do Postgres (já compilado dentro da
-- imagem `postgres:16-alpine` usada localmente e na nuvem — ver
-- docker-compose.yml e infra/modules/database/main.tf) — só precisa ser
-- habilitado, nada de imagem customizada.
CREATE EXTENSION IF NOT EXISTS intarray;

-- Uma linha por contexto (C=20, ver data_generation/README.md), com TODOS os
-- itens daquele contexto — não truncado, mesma fonte de
-- inverted_lists.parquet (harness/fixtures.py:load_inverted_lists). Sem
-- índice GIN: o acesso é sempre direto por context_id (chave primária); GIN
-- só aceleraria "quais linhas contêm o item X", que não é o padrão de acesso
-- de intersect.
CREATE TABLE IF NOT EXISTS inverted_lists (
    context_id SMALLINT PRIMARY KEY,
    item_ids INT[] NOT NULL
);
