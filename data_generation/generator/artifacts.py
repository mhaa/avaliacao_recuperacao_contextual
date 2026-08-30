"""Etapa 4 — artefatos derivados: listas invertidas (+ bitmaps Roaring, E-4) e
pré-materialização por (usuário, contexto) (E-3)."""

from __future__ import annotations

import shutil
from pathlib import Path

import polars as pl
from pyroaring import BitMap

from . import config, contexts as contexts_module, ingest
from .io_utils import update_stats, write_parquet_deterministic
from .paths import DataPaths, read_stats, stage_is_done


def build_inverted_lists(
    contexts_df: pl.DataFrame, combination_membership: pl.DataFrame
) -> pl.DataFrame:
    joined = contexts_module.context_item_pairs(contexts_df, combination_membership)
    lists_df = joined.group_by("context_id").agg(pl.col("item_id").sort().alias("item_ids"))
    return (
        contexts_df.select("context_id")
        .join(lists_df, on="context_id", how="left")
        .with_columns(pl.col("item_ids").fill_null([]))
        .sort("context_id")
    )


def write_roaring_bitmaps(inverted_lists_df: pl.DataFrame, out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for row in inverted_lists_df.iter_rows(named=True):
        bitmap = BitMap(row["item_ids"])
        (out_dir / f"{row['context_id']}.bin").write_bytes(bitmap.serialize())


def build_prematerialized(
    candidates_lf: pl.LazyFrame,
    contexts_df: pl.DataFrame,
    combination_membership: pl.DataFrame,
    n_users: int,
    m: int,
) -> pl.DataFrame:
    """Uma linha por (usuário, contexto), inclusive sem match — top-M por
    rank, truncado em m. Garante exatamente n_users * C linhas."""
    context_item_map = contexts_module.context_item_pairs(contexts_df, combination_membership)

    # Processa em lotes de usuário, não a base inteira de uma vez: em
    # escala real (U=200.948), o join (candidates × pertences item-contexto,
    # ~100M+ linhas de entrada) + group_by materializado de uma só vez
    # estourou memória (SIGKILL, nem `engine="streaming"` do polars nem 9GB
    # livres no Docker Desktop resolveram — confirmado travando duas vezes
    # seguidas, sempre no mesmo ponto, nunca chegando a escrever
    # prematerialized.parquet). Lotes do tamanho da escala de
    # desenvolvimento (10.000 usuários, que sempre funcionou sem esforço)
    # limitam o pico de memória por lote independentemente de U.
    batch_size = 10_000
    topm_batches = [
        candidates_lf.filter(pl.col("user_id").is_between(lo, lo + batch_size, closed="left"))
        .join(context_item_map.lazy(), on="item_id", how="inner")
        .group_by(["user_id", "context_id"])
        .agg(
            pl.col("item_id").sort_by("rank").head(m).alias("item_ids"),
            pl.col("score").sort_by("rank").head(m).alias("scores"),
        )
        .collect()
        for lo in range(0, n_users, batch_size)
    ]
    topm = pl.concat(topm_batches)

    grid = pl.DataFrame(
        {"user_id": list(range(n_users))}, schema={"user_id": pl.Int32}
    ).join(contexts_df.select("context_id"), how="cross")

    return (
        grid.join(topm, on=["user_id", "context_id"], how="left")
        .with_columns(pl.col("item_ids").fill_null([]), pl.col("scores").fill_null([]))
        .select(["user_id", "context_id", "item_ids", "scores"])
    )


def run(data_dir: DataPaths, sample_users: int | None, seed: int, force: bool) -> dict:
    params = {"sample_users": sample_users, "seed": seed}
    outputs = [data_dir.inverted_lists, data_dir.prematerialized, data_dir.inverted_bitmaps_dir]
    if not force and stage_is_done("artifacts", outputs, data_dir.stats, params):
        return read_stats(data_dir.stats).get("artifacts", {})

    ingest_stats = read_stats(data_dir.stats)["ingest"]
    n_users = ingest_stats["U"]

    id_maps = pl.read_parquet(data_dir.id_maps)
    movies_lf = ingest.read_movies_raw(data_dir.raw_dir)

    genre_membership = contexts_module.parse_genres(movies_lf, id_maps)
    combination_membership = contexts_module.build_combination_membership(
        genre_membership, config.MAX_GENRE_COMBINATION_SIZE
    )
    contexts_df = pl.read_parquet(data_dir.contexts)

    inverted_lists_df = build_inverted_lists(contexts_df, combination_membership)
    write_parquet_deterministic(inverted_lists_df, data_dir.inverted_lists, sort_by=["context_id"])
    write_roaring_bitmaps(inverted_lists_df, data_dir.inverted_bitmaps_dir)

    candidates_lf = pl.scan_parquet(str(data_dir.candidates_dir / "*.parquet"))
    prematerialized_df = build_prematerialized(
        candidates_lf, contexts_df, combination_membership, n_users, config.M_PREMATERIALIZED
    )
    write_parquet_deterministic(
        prematerialized_df, data_dir.prematerialized, sort_by=["user_id", "context_id"]
    )

    fill_counts = (
        prematerialized_df.with_columns(pl.col("item_ids").list.len().alias("_n"))
        .group_by("_n")
        .agg(pl.len().alias("count"))
        .sort("_n")
    )
    fill_distribution = {
        str(k): v for k, v in zip(fill_counts["_n"].to_list(), fill_counts["count"].to_list())
    }

    stats = {
        "params": params,
        "inverted_lists_contexts": inverted_lists_df.height,
        "prematerialized_rows": prematerialized_df.height,
        "prematerialized_expected_rows": n_users * config.C_CONTEXTS,
        "prematerialized_fill_distribution": fill_distribution,
    }
    update_stats(data_dir.stats, "artifacts", stats)
    return stats
