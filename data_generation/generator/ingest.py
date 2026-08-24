"""Etapa 1 — ingestão dos CSVs brutos do MovieLens e densificação de IDs.

Os movieId do MovieLens são esparsos; mapas de bits e listas invertidas
dependem de IDs densos (0..I-1, 0..U-1) para não desperdiçar espaço.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl

from . import download
from .io_utils import update_stats, write_parquet_deterministic
from .paths import DataPaths, read_stats, stage_is_done


def read_ratings_raw(raw_dir: Path) -> pl.LazyFrame:
    return pl.scan_csv(raw_dir / "ratings.csv")


def read_movies_raw(raw_dir: Path) -> pl.LazyFrame:
    return pl.scan_csv(raw_dir / "movies.csv")


def select_sampled_users(ratings_lf: pl.LazyFrame, n: int, seed: int) -> pl.Series:
    """Subamostra determinística de userId distintos, sem reposição."""
    unique_ids = ratings_lf.select(pl.col("userId").unique()).collect().to_series()
    ids_np = unique_ids.to_numpy()
    rng = np.random.default_rng(seed)
    n = min(n, len(ids_np))
    idx = rng.choice(len(ids_np), size=n, replace=False)
    return pl.Series("userId", np.sort(ids_np[idx]))


def build_id_maps(
    ratings_lf: pl.LazyFrame,
    movies_lf: pl.LazyFrame,
    sampled_user_ids: pl.Series | None = None,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Universo de itens = todo movieId de movies.csv (independe da amostra).
    Universo de usuários = userId distintos em ratings, filtrado pela amostra.
    """
    item_ids = (
        movies_lf.select(pl.col("movieId").unique().cast(pl.Int64))
        .collect()
        .to_series()
        .sort()
    )
    item_map = pl.DataFrame(
        {
            "original_id": item_ids,
            "dense_id": pl.arange(0, item_ids.len(), eager=True).cast(pl.Int32),
        }
    )

    users_lf = ratings_lf.select(pl.col("userId").unique().cast(pl.Int64))
    if sampled_user_ids is not None:
        users_lf = users_lf.filter(pl.col("userId").is_in(sampled_user_ids.to_list()))
    user_ids = users_lf.collect().to_series().sort()
    user_map = pl.DataFrame(
        {
            "original_id": user_ids,
            "dense_id": pl.arange(0, user_ids.len(), eager=True).cast(pl.Int32),
        }
    )

    return user_map, item_map


def write_id_maps(user_map: pl.DataFrame, item_map: pl.DataFrame, path: Path) -> None:
    combined = pl.concat(
        [
            user_map.with_columns(pl.lit("user").alias("entity")),
            item_map.with_columns(pl.lit("item").alias("entity")),
        ]
    ).select(["entity", "original_id", "dense_id"])
    write_parquet_deterministic(combined, path, sort_by=["entity", "dense_id"])


def load_dense_ratings(raw_dir: Path, id_maps_path: Path) -> pl.LazyFrame:
    """Ratings brutas com user_id/item_id densos, restritas ao id_maps existente.

    Reutilizado por rank.py — evita persistir um dataset intermediário redundante.
    """
    id_maps = pl.read_parquet(id_maps_path)
    user_map = id_maps.filter(pl.col("entity") == "user").select(
        pl.col("original_id").alias("userId"), pl.col("dense_id").alias("user_id")
    )
    item_map = id_maps.filter(pl.col("entity") == "item").select(
        pl.col("original_id").alias("movieId"), pl.col("dense_id").alias("item_id")
    )
    ratings_lf = read_ratings_raw(raw_dir).with_columns(
        pl.col("userId").cast(pl.Int64), pl.col("movieId").cast(pl.Int64)
    )
    return (
        ratings_lf.join(user_map.lazy(), on="userId", how="inner")
        .join(item_map.lazy(), on="movieId", how="inner")
        .select(["user_id", "item_id", "rating", "timestamp"])
    )


def run(data_dir: DataPaths, sample_users: int | None, seed: int, force: bool) -> dict:
    params = {"sample_users": sample_users, "seed": seed}
    if not force and stage_is_done("ingest", [data_dir.id_maps], data_dir.stats, params):
        return read_stats(data_dir.stats).get("ingest", {})

    download.ensure_dataset(data_dir.raw_dir)

    ratings_lf = read_ratings_raw(data_dir.raw_dir)
    movies_lf = read_movies_raw(data_dir.raw_dir)

    sampled_user_ids = None
    if sample_users is not None:
        sampled_user_ids = select_sampled_users(ratings_lf, sample_users, seed)

    user_map, item_map = build_id_maps(ratings_lf, movies_lf, sampled_user_ids)
    write_id_maps(user_map, item_map, data_dir.id_maps)

    raw_ratings_rows = ratings_lf.select(pl.len()).collect().item()
    raw_movies_rows = movies_lf.select(pl.len()).collect().item()
    dense_ratings_rows = (
        load_dense_ratings(data_dir.raw_dir, data_dir.id_maps)
        .select(pl.len())
        .collect()
        .item()
    )

    stats = {
        "params": params,
        "U": user_map.height,
        "I": item_map.height,
        "raw_ratings_rows": raw_ratings_rows,
        "raw_movies_rows": raw_movies_rows,
        "dense_ratings_rows": dense_ratings_rows,
        "dropped_ratings_rows": raw_ratings_rows - dense_ratings_rows,
    }
    update_stats(data_dir.stats, "ingest", stats)
    return stats
