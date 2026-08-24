"""Etapa 2 — ranking offline via ALS (Hu et al., 2008), biblioteca `implicit`.

Parâmetros do ALS não são variáveis do estudo (config.py) — não ajustar.
GPU e BLAS multi-thread não são bit-reprodutíveis por padrão; fixamos
single-thread via threadpoolctl para o critério de aceitação "reexecução
idêntica".
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterator

import numpy as np
import polars as pl
import scipy.sparse as sp
import threadpoolctl
from implicit.als import AlternatingLeastSquares

from . import config
from .ingest import load_dense_ratings
from .io_utils import update_stats, write_parquet_deterministic
from .paths import DataPaths, read_stats, stage_is_done
from .seeds import derive_seeds


def ratings_to_confidence(
    ratings_df: pl.DataFrame,
    n_users: int,
    n_items: int,
    alpha: int = config.ALS_CONFIDENCE_ALPHA,
) -> sp.csr_matrix:
    """confidence = 1 + alpha * rating (convenção Hu et al.)."""
    rows = ratings_df["user_id"].to_numpy()
    cols = ratings_df["item_id"].to_numpy()
    confidence = 1.0 + alpha * ratings_df["rating"].to_numpy()
    return sp.csr_matrix(
        (confidence.astype(np.float32), (rows, cols)), shape=(n_users, n_items)
    )


def train_als(
    user_items: sp.csr_matrix,
    factors: int,
    regularization: float,
    iterations: int,
    random_state: int,
) -> AlternatingLeastSquares:
    model = AlternatingLeastSquares(
        factors=factors,
        regularization=regularization,
        iterations=iterations,
        random_state=random_state,
        use_gpu=False,
        num_threads=1,
    )
    with threadpoolctl.threadpool_limits(1):
        model.fit(user_items, show_progress=False)
    return model


def compute_item_popularity(ratings_df: pl.DataFrame, n_items: int) -> pl.DataFrame:
    """Popularidade de TODO o catálogo (0..n_items-1), não só itens avaliados —
    itens nunca avaliados entram com count=0, para que o backfill sempre
    consiga completar até N mesmo em catálogos pequenos/esparsos."""
    counts = ratings_df.group_by("item_id").agg(pl.len().alias("count"))
    all_items = pl.DataFrame({"item_id": pl.arange(0, n_items, eager=True).cast(pl.Int32)})
    return (
        all_items.join(counts, on="item_id", how="left")
        .with_columns(pl.col("count").fill_null(0))
        .sort(["count", "item_id"], descending=[True, False])
    )


def _flatten_recommend_output(
    user_ids: np.ndarray,
    ids2d: np.ndarray,
    scores2d: np.ndarray,
    valid_counts: np.ndarray,
) -> pl.DataFrame:
    n_request = ids2d.shape[1]
    mask = np.arange(n_request)[None, :] < valid_counts[:, None]
    flat_user = np.repeat(user_ids, valid_counts).astype(np.int32)
    return pl.DataFrame(
        {
            "user_id": flat_user,
            "item_id": ids2d[mask].astype(np.int32),
            "score": scores2d[mask].astype(np.float32),
        }
    )


def recommend_blocks(
    model: AlternatingLeastSquares,
    user_items: sp.csr_matrix,
    user_ids: np.ndarray,
    n: int,
    block_size: int,
) -> Iterator[pl.DataFrame]:
    n_items = user_items.shape[1]
    n_request = min(n, n_items)
    with threadpoolctl.threadpool_limits(1):
        for start in range(0, len(user_ids), block_size):
            block_users = user_ids[start : start + block_size]
            block_matrix = user_items[block_users]
            ids2d, scores2d = model.recommend(
                block_users,
                block_matrix,
                N=n_request,
                filter_already_liked_items=True,
            )
            liked_counts = np.diff(block_matrix.indptr)
            valid_counts = np.clip(n_items - liked_counts, 0, n_request)
            yield _flatten_recommend_output(block_users, ids2d, scores2d, valid_counts)


def backfill_popular(
    candidates_df: pl.DataFrame,
    popularity_df: pl.DataFrame,
    user_items: sp.csr_matrix,
    n: int,
) -> tuple[pl.DataFrame, dict[int, int]]:
    """Completa usuários com menos de n candidatos com os itens mais
    populares ainda não avaliados/recomendados. Tie-break item_id asc."""
    counts = candidates_df.group_by("user_id").agg(pl.len().alias("count"))
    short = counts.filter(pl.col("count") < n)
    if short.height == 0:
        return candidates_df, {}

    popularity_ids = popularity_df["item_id"].to_list()
    short_user_ids = short["user_id"].to_list()
    existing = (
        candidates_df.filter(pl.col("user_id").is_in(short_user_ids))
        .group_by("user_id")
        .agg(pl.col("item_id"), pl.col("score").min().alias("min_score"))
    )
    existing_by_user = {
        row["user_id"]: (set(row["item_id"]), row["min_score"])
        for row in existing.iter_rows(named=True)
    }

    backfill_counts: dict[int, int] = {}
    extra_parts = []
    for row in short.iter_rows(named=True):
        u = row["user_id"]
        needed = n - row["count"]
        current_items, min_score = existing_by_user[u]
        already_rated = set(user_items[u].indices.tolist())
        exclude = current_items | already_rated

        picked = []
        for item in popularity_ids:
            if item not in exclude:
                picked.append(item)
                if len(picked) == needed:
                    break
        backfill_counts[u] = len(picked)
        if not picked:
            continue
        base_score = min_score if min_score is not None else 0.0
        scores = [base_score - 1.0 - k for k in range(len(picked))]
        extra_parts.append(
            pl.DataFrame(
                {
                    "user_id": np.full(len(picked), u, dtype=np.int32),
                    "item_id": np.array(picked, dtype=np.int32),
                    "score": np.array(scores, dtype=np.float32),
                }
            )
        )

    if extra_parts:
        candidates_df = pl.concat([candidates_df, *extra_parts])
    return candidates_df, backfill_counts


def assign_ranks(df: pl.DataFrame) -> pl.DataFrame:
    """rank 1..N por usuário; tie-break score desc, item_id asc."""
    return df.sort(
        ["user_id", "score", "item_id"], descending=[False, True, False]
    ).with_columns(
        pl.int_range(1, pl.len() + 1).over("user_id").cast(pl.Int16).alias("rank")
    )


def write_candidates(blocks_iter: Iterator[pl.DataFrame], out_dir: Path) -> int:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    total_rows = 0
    for i, block_df in enumerate(blocks_iter):
        path = out_dir / f"block_{i:04d}.parquet"
        write_parquet_deterministic(block_df, path, sort_by=["user_id", "rank"])
        total_rows += block_df.height
    return total_rows


def run(data_dir: DataPaths, sample_users: int | None, seed: int, force: bool) -> dict:
    params = {"sample_users": sample_users, "seed": seed}
    if not force and stage_is_done("rank", [data_dir.candidates_dir], data_dir.stats, params):
        return read_stats(data_dir.stats).get("rank", {})

    ingest_stats = read_stats(data_dir.stats).get("ingest", {})
    n_users, n_items = ingest_stats["U"], ingest_stats["I"]

    ratings_df = load_dense_ratings(data_dir.raw_dir, data_dir.id_maps).collect()
    user_items = ratings_to_confidence(ratings_df, n_users, n_items)
    popularity_df = compute_item_popularity(ratings_df, n_items)

    als_seed = derive_seeds(seed)["als"]
    model = train_als(
        user_items,
        factors=config.ALS_FACTORS,
        regularization=config.ALS_REGULARIZATION,
        iterations=config.ALS_ITERATIONS,
        random_state=als_seed,
    )

    all_user_ids = np.arange(n_users, dtype=np.int64)

    def ranked_blocks() -> Iterator[pl.DataFrame]:
        total_backfilled_users = 0
        total_backfilled_items = 0
        for block_df in recommend_blocks(
            model, user_items, all_user_ids, config.N_CANDIDATES, config.CANDIDATES_BLOCK_SIZE
        ):
            block_df, backfill_counts = backfill_popular(
                block_df, popularity_df, user_items, config.N_CANDIDATES
            )
            nonlocal_stats["backfilled_users"] += len(backfill_counts)
            nonlocal_stats["backfilled_items"] += sum(backfill_counts.values())
            yield assign_ranks(block_df)

    nonlocal_stats = {"backfilled_users": 0, "backfilled_items": 0}
    total_rows = write_candidates(ranked_blocks(), data_dir.candidates_dir)

    stats = {
        "params": params,
        "als": {
            "factors": config.ALS_FACTORS,
            "regularization": config.ALS_REGULARIZATION,
            "iterations": config.ALS_ITERATIONS,
            "confidence_formula": "1 + alpha * rating",
            "confidence_alpha": config.ALS_CONFIDENCE_ALPHA,
            "random_state": als_seed,
            "use_gpu": False,
            "num_threads": 1,
        },
        "total_candidate_rows": total_rows,
        "backfilled_users": nonlocal_stats["backfilled_users"],
        "backfilled_items": nonlocal_stats["backfilled_items"],
    }
    update_stats(data_dir.stats, "rank", stats)
    return stats
