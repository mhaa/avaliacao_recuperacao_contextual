-- Esquema mínimo para BD-1 (Postgres), suficiente para as primitivas já
-- implementadas em storage/postgres.py: get_candidates e
-- get_candidates_filtered. Tabelas de pré-materialização (E-3) e o que
-- mais E-4 precisar chegam na Etapa 4 (ver docs/ARCHITECTURE.md).
--
-- Rodado automaticamente pela imagem oficial do Postgres via
-- docker-entrypoint-initdb.d (ver docker-compose.yml) — só DDL aqui, nunca
-- dado de teste ou de produção.

CREATE TABLE IF NOT EXISTS candidates (
    user_id INT NOT NULL,
    item_id INT NOT NULL,
    rank SMALLINT NOT NULL,
    score REAL NOT NULL,
    PRIMARY KEY (user_id, item_id)
);

-- INCLUDE (item_id, score): as consultas de storage/postgres.py que usam
-- este índice (`get_candidates`, `get_candidates_filtered`, `intersect`)
-- leem exatamente essas duas colunas além da chave — com elas no índice o
-- plano vira index-only scan, evitando até N=500 buscas na heap por
-- requisição. Neutro quanto à estratégia da célula: E-1/E-2/E-4 leem as
-- mesmas colunas, nenhuma ganha vantagem sobre a outra.
CREATE INDEX IF NOT EXISTS idx_candidates_user_rank
    ON candidates (user_id, rank) INCLUDE (item_id, score);

-- Pertença item -> contexto (dado de catálogo, C=20 contextos materializados
-- por generator/contexts.py — ver data_generation/README.md).
CREATE TABLE IF NOT EXISTS item_contexts (
    item_id INT NOT NULL,
    context_id SMALLINT NOT NULL,
    PRIMARY KEY (item_id, context_id)
);

CREATE INDEX IF NOT EXISTS idx_item_contexts_context ON item_contexts (context_id);
