"""Dados necessários para verificar os 1000 casos do oráculo contra uma
célula — não a base inteira (10.000 usuários x 500 candidatos seria
desperdício só para este propósito). Compartilhado por
`schemas/<db>/load_oracle_fixture.py` de cada banco, para não duplicar esta
lógica de carregamento (polars) a cada tecnologia nova.

`load_candidates`/`load_prematerialized` também servem
`schemas/<db>/load_full_dataset.py` (Fase 5, bateria de medição real) —
passar `user_ids=None` pula o filtro e carrega a base inteira, sem duplicar
a lógica polars entre o carregador do oráculo e o completo.
"""

from __future__ import annotations

import os

import polars as pl
from google.cloud import storage

DATA_DIR = "data_generation/data"


def needed_user_ids() -> list[int]:
    oracle = pl.read_parquet(f"{DATA_DIR}/oracle.parquet")
    return oracle["user_id"].unique().to_list()


def ensure_full_dataset_downloaded() -> None:
    """Baixa `candidates.parquet/` e `prematerialized.parquet` do bucket
    `DATASET_BUCKET` (env var) para `DATA_DIR`, se ainda não estiverem lá —
    só usado pelos loaders completos da Fase 5 (nunca pelo fixture do
    oráculo, pequeno o bastante para já vir embutido em
    `docker/Dockerfile.tools`). Idempotente: um segundo `terraform apply`
    da mesma célula (ex.: reaplicar depois de uma falha) não baixa de novo.
    Credenciais via ADC — o `docker run` da VM `loadgen` já roda com
    `--network host`, então o cliente enxerga o metadata server da VM e
    minta um token da conta de serviço automaticamente, sem repetir o
    padrão curl+token já usado nos startup scripts do Terraform."""
    bucket_name = os.environ["DATASET_BUCKET"]
    client = storage.Client()
    bucket = client.bucket(bucket_name)

    prematerialized_path = f"{DATA_DIR}/prematerialized.parquet"
    if not os.path.exists(prematerialized_path):
        bucket.blob("dataset/prematerialized.parquet").download_to_filename(
            prematerialized_path
        )

    candidates_dir = f"{DATA_DIR}/candidates.parquet"
    os.makedirs(candidates_dir, exist_ok=True)
    if not os.listdir(candidates_dir):
        for blob in bucket.list_blobs(prefix="dataset/candidates.parquet/"):
            filename = blob.name.rsplit("/", 1)[-1]
            blob.download_to_filename(f"{candidates_dir}/{filename}")


def load_candidates(user_ids: list[int] | None = None) -> pl.DataFrame:
    lf = pl.scan_parquet(f"{DATA_DIR}/candidates.parquet/*.parquet")
    if user_ids is not None:
        lf = lf.filter(pl.col("user_id").is_in(user_ids))
    return lf.collect()


def load_prematerialized(user_ids: list[int] | None = None) -> pl.DataFrame:
    """prematerialized.parquet guarda item_ids/scores como listas (top-40
    por par usuário-contexto, já ordenadas por rank — ver
    data_generation/README.md); explode em uma linha por item, com rank =
    posição na lista."""
    df = pl.read_parquet(f"{DATA_DIR}/prematerialized.parquet")
    if user_ids is not None:
        df = df.filter(pl.col("user_id").is_in(user_ids))
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
