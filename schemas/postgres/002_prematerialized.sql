-- Tabela de pré-materialização para E-3 — uma linha por
-- (usuário, contexto único, item), até M=40 itens por par (ver
-- data_generation/README.md: M=40, não 20, porque a exclusão de sessão é
-- aplicada em tempo de requisição sobre o resultado lido).

CREATE TABLE IF NOT EXISTS prematerialized (
    user_id INT NOT NULL,
    context_id SMALLINT NOT NULL,
    item_id INT NOT NULL,
    rank SMALLINT NOT NULL,
    score REAL NOT NULL,
    PRIMARY KEY (user_id, context_id, item_id)
);

-- INCLUDE (item_id, score): mesma razão de idx_candidates_user_rank em
-- 001_schema.sql — `get_prematerialized` lê só essas duas colunas além da
-- chave, então o índice cobre a consulta inteira (index-only scan).
CREATE INDEX IF NOT EXISTS idx_prematerialized_lookup
    ON prematerialized (user_id, context_id, rank) INCLUDE (item_id, score);
