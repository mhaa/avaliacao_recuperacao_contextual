"""Dados necessários para verificar os 1000 casos do oráculo contra uma
célula — não a base inteira (10.000 usuários x 500 candidatos seria
desperdício só para este propósito). Compartilhado por
`schemas/<db>/load_oracle_fixture.py` de cada banco, para não duplicar esta
lógica de carregamento (polars) a cada tecnologia nova.
"""

from __future__ import annotations

import polars as pl

DATA_DIR = "data_generation/data"


def needed_user_ids() -> list[int]:
    oracle = pl.read_parquet(f"{DATA_DIR}/oracle.parquet")
    return oracle["user_id"].unique().to_list()


def load_candidates(user_ids: list[int]) -> pl.DataFrame:
    lf = pl.scan_parquet(f"{DATA_DIR}/candidates.parquet/*.parquet")
    return lf.filter(pl.col("user_id").is_in(user_ids)).collect()


def load_prematerialized(user_ids: list[int]) -> pl.DataFrame:
    """prematerialized.parquet guarda item_ids/scores como listas (top-40
    por par usuário-contexto, já ordenadas por rank — ver
    data_generation/README.md); explode em uma linha por item, com rank =
    posição na lista."""
    df = pl.read_parquet(f"{DATA_DIR}/prematerialized.parquet").filter(
        pl.col("user_id").is_in(user_ids)
    )
    df = df.with_columns(pl.int_ranges(1, pl.col("item_ids").list.len() + 1).alias("ranks"))
    return (
        df.explode(["item_ids", "scores", "ranks"])
        .filter(pl.col("item_ids").is_not_null())
        .select(
            "user_id",
            "context_id",
            pl.col("item_ids").alias("item_id"),
            pl.col("ranks").alias("rank"),
            pl.col("scores").alias("score"),
        )
    )


def load_item_contexts() -> pl.DataFrame:
    """Recalcula a pertença item->contexto a partir de items.parquet
    (vocabulário completo de gêneros por item) e contexts.parquet (genre_ids
    de cada um dos 20 contextos materializados): um item pertence a um
    contexto quando seus gêneros contêm TODOS os genre_ids do contexto —
    mesma regra AND de generator/contexts.py, recalculada aqui (nunca lida
    de inverted_lists.parquet/prematerialized.parquet, que são os próprios
    artefatos de E-3/E-4 a serem testados contra este oráculo)."""
    items = pl.read_parquet(f"{DATA_DIR}/items.parquet").select(["item_id", "genres"])
    contexts = pl.read_parquet(f"{DATA_DIR}/contexts.parquet").select(["context_id", "genre_ids"])

    items_exploded = items.explode("genres").rename({"genres": "genre_id"})
    contexts_exploded = contexts.explode("genre_ids").rename({"genre_ids": "genre_id"})
    context_sizes = contexts.with_columns(pl.col("genre_ids").list.len().alias("n_genres"))

    return (
        items_exploded.join(contexts_exploded, on="genre_id", how="inner")
        .group_by(["item_id", "context_id"])
        .agg(pl.len().alias("matched_genres"))
        .join(context_sizes.select(["context_id", "n_genres"]), on="context_id")
        .filter(pl.col("matched_genres") == pl.col("n_genres"))
        .select(["item_id", "context_id"])
    )


def load_inverted_lists() -> pl.DataFrame:
    """inverted_lists.parquet já é o artefato completo (não truncado) de
    catálogo — pequeno o bastante (~360 KB) para carregar por inteiro, sem
    precisar filtrar por usuário como as outras funções deste módulo."""
    return pl.read_parquet(f"{DATA_DIR}/inverted_lists.parquet")
