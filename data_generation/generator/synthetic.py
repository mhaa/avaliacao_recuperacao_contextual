"""Expansão sintética — aplicada apenas à varredura de escalabilidade, não ao
experimento principal (as três fases rodam sobre a base real).

Por escala, só os artefatos dependentes de usuário são regenerados
(candidates, contexts, prematerialized); items.parquet e inverted_lists/
bitmaps são de catálogo e não variam com o número de usuários.
"""

from __future__ import annotations

from typing import Iterator

import numpy as np
import polars as pl

from . import artifacts, config, contexts as contexts_module, ingest, rank
from .io_utils import update_stats, write_parquet_deterministic
from .paths import DataPaths, read_stats
from .seeds import derive_seeds


def sample_synthetic_profiles(
    rng: np.random.Generator, real_user_ids: np.ndarray, scale: int
) -> np.ndarray:
    return rng.choice(real_user_ids, size=scale, replace=True)


def inject_popularity_noise(
    profile_candidates: pl.DataFrame,
    p: float,
    popularity_df: pl.DataFrame,
    rng: np.random.Generator,
) -> pl.DataFrame:
    """Substitui round(p*n) dos candidatos do perfil por itens sorteados pela
    popularidade empírica do catálogo. Ordem relativa dos remanescentes
    preservada; novos itens inseridos em posições sorteadas; scores
    reatribuídos por interpolação monótona no range original do perfil."""
    ordered = profile_candidates.sort("rank")
    n = ordered.height
    n_replace = round(p * n)

    item_ids = ordered["item_id"].to_numpy()
    scores = ordered["score"].to_numpy()

    if n_replace == 0:
        return pl.DataFrame(
            {
                "item_id": item_ids.astype(np.int32),
                "rank": np.arange(1, n + 1, dtype=np.int16),
                "score": scores.astype(np.float32),
            }
        )

    replace_idx = rng.choice(n, size=n_replace, replace=False)
    replace_mask = np.zeros(n, dtype=bool)
    replace_mask[replace_idx] = True
    kept_items = item_ids[~replace_mask]

    pop_ids = popularity_df["item_id"].to_numpy()
    pop_weights = popularity_df["count"].to_numpy().astype(np.float64) + 1.0
    pop_weights = pop_weights / pop_weights.sum()

    exclude = set(int(x) for x in kept_items)
    draw_size = min(len(pop_ids), n_replace + len(exclude) + 10)
    drawn = rng.choice(pop_ids, size=draw_size, replace=False, p=pop_weights)
    new_items = [int(x) for x in drawn if int(x) not in exclude][:n_replace]
    if len(new_items) < n_replace:
        remaining = [int(i) for i in pop_ids if int(i) not in exclude and int(i) not in new_items]
        new_items.extend(remaining[: n_replace - len(new_items)])

    new_positions = np.sort(rng.choice(n, size=n_replace, replace=False))
    is_new_slot = np.zeros(n, dtype=bool)
    is_new_slot[new_positions] = True

    final_items = np.empty(n, dtype=np.int64)
    final_items[is_new_slot] = new_items
    final_items[~is_new_slot] = kept_items

    score_max, score_min = float(scores.max()), float(scores.min())
    final_scores = np.linspace(score_max, score_min, n) if n > 1 else np.array([score_max])

    return pl.DataFrame(
        {
            "item_id": final_items.astype(np.int32),
            "rank": np.arange(1, n + 1, dtype=np.int16),
            "score": final_scores.astype(np.float32),
        }
    )


def _synthetic_stage_key(scale: int) -> str:
    return str(scale)


def build_scale(data_dir: DataPaths, scale: int, p: float, seed: int, force: bool) -> dict:
    scale_key = _synthetic_stage_key(scale)
    synth_paths = DataPaths(data_dir.synthetic_dir(scale))
    outputs = [synth_paths.candidates_dir, synth_paths.contexts, synth_paths.prematerialized]
    params = {"scale": scale, "p": p, "seed": seed}

    stats = read_stats(data_dir.stats)
    existing = stats.get("synthetic", {}).get(scale_key, {})
    if not force and all(o.exists() for o in outputs) and existing.get("params") == params:
        return existing

    ingest_stats = stats["ingest"]
    n_users_real = ingest_stats["U"]
    n_items = ingest_stats["I"]

    id_maps = pl.read_parquet(data_dir.id_maps)
    ratings_df = ingest.load_dense_ratings(data_dir.raw_dir, data_dir.id_maps).collect()
    popularity_df = rank.compute_item_popularity(ratings_df, n_items)

    real_candidates = pl.scan_parquet(str(data_dir.candidates_dir / "*.parquet")).collect()
    real_candidates_by_user = {
        key[0]: sub for key, sub in real_candidates.group_by("user_id", maintain_order=True)
    }

    seed_map = derive_seeds(seed)
    profile_rng = np.random.default_rng(seed_map["synthetic_sampling"])
    injection_rng = np.random.default_rng(seed_map["synthetic_injection"])

    profiles = sample_synthetic_profiles(profile_rng, np.arange(n_users_real), scale)

    def synthetic_blocks() -> Iterator[pl.DataFrame]:
        rows: list[pl.DataFrame] = []
        for synth_user_id in range(scale):
            base_user = int(profiles[synth_user_id])
            base_df = real_candidates_by_user[base_user]
            synth_df = inject_popularity_noise(base_df, p, popularity_df, injection_rng)
            rows.append(synth_df.with_columns(pl.lit(synth_user_id).cast(pl.Int32).alias("user_id")))
            if len(rows) >= config.CANDIDATES_BLOCK_SIZE:
                yield pl.concat(rows).select(["user_id", "item_id", "rank", "score"])
                rows = []
        if rows:
            yield pl.concat(rows).select(["user_id", "item_id", "rank", "score"])

    total_rows = rank.write_candidates(synthetic_blocks(), synth_paths.candidates_dir)

    movies_lf = ingest.read_movies_raw(data_dir.raw_dir)
    genre_membership = contexts_module.parse_genres(movies_lf, id_maps)
    real_contexts_df = pl.read_parquet(data_dir.contexts)
    combination_membership = contexts_module.build_combination_membership(
        genre_membership, config.MAX_GENRE_COMBINATION_SIZE
    )

    synthetic_candidates_lf = pl.scan_parquet(str(synth_paths.candidates_dir / "*.parquet"))
    sample_ids = contexts_module.sample_users_for_selectivity(scale, seed)
    sample_ids_series = pl.Series("user_id", sample_ids).cast(pl.Int32)
    selectivity_df = contexts_module.compute_membership_selectivity(
        synthetic_candidates_lf, combination_membership, sample_ids_series
    )
    synthetic_contexts_df = (
        real_contexts_df.select(["context_id", "genre_ids", "label", "catalog_fraction", "tier"])
        .with_columns(contexts_module._combo_key_expr())
        .join(selectivity_df, on="combo_key", how="left")
        .select(
            ["context_id", "genre_ids", "label", "catalog_fraction", "candidate_selectivity", "tier"]
        )
    )
    write_parquet_deterministic(synthetic_contexts_df, synth_paths.contexts, sort_by=["context_id"])

    prematerialized_df = artifacts.build_prematerialized(
        synthetic_candidates_lf,
        synthetic_contexts_df,
        combination_membership,
        scale,
        config.M_PREMATERIALIZED,
    )
    write_parquet_deterministic(
        prematerialized_df, synth_paths.prematerialized, sort_by=["user_id", "context_id"]
    )

    scale_stats = {
        "params": params,
        "n_synthetic_users": scale,
        "total_candidate_rows": total_rows,
    }
    stats.setdefault("synthetic", {})[scale_key] = scale_stats
    update_stats(data_dir.stats, "synthetic", stats["synthetic"])
    return scale_stats


def _popularity_cumulative_summary(candidates_lf: pl.LazyFrame) -> dict[str, float]:
    counts = (
        candidates_lf.group_by("item_id")
        .agg(pl.len().alias("count"))
        .sort("count", descending=True)
        .collect()
    )
    total = counts["count"].sum()
    cum = counts["count"].cum_sum() / total if total else counts["count"].cum_sum()
    n = counts.height
    summary = {}
    for pct in (0.01, 0.05, 0.10):
        idx = max(int(n * pct) - 1, 0)
        summary[f"top_{int(pct * 100)}pct_share"] = float(cum[idx]) if n else 0.0
    return summary


def _mean_distinct_contexts_per_user(
    candidates_lf: pl.LazyFrame, contexts_df: pl.DataFrame, membership: pl.DataFrame, sample_user_ids: pl.Series
) -> float:
    context_item_map = contexts_module.context_item_pairs(contexts_df, membership)
    sampled = candidates_lf.filter(pl.col("user_id").is_in(sample_user_ids.to_list()))
    per_user = (
        sampled.join(context_item_map.lazy(), on="item_id", how="inner")
        .select(["user_id", "context_id"])
        .unique()
        .group_by("user_id")
        .agg(pl.len().alias("n_contexts"))
        .collect()
    )
    if per_user.height == 0:
        return 0.0
    return float(per_user["n_contexts"].mean())


def compare_real_vs_synthetic(data_dir: DataPaths, scale: int, seed: int) -> dict:
    synth_paths = DataPaths(data_dir.synthetic_dir(scale))
    stats = read_stats(data_dir.stats)
    n_users_real = stats["ingest"]["U"]

    id_maps = pl.read_parquet(data_dir.id_maps)
    movies_lf = ingest.read_movies_raw(data_dir.raw_dir)
    genre_membership = contexts_module.parse_genres(movies_lf, id_maps)
    combination_membership = contexts_module.build_combination_membership(
        genre_membership, config.MAX_GENRE_COMBINATION_SIZE
    )

    real_contexts_df = pl.read_parquet(data_dir.contexts)
    synthetic_contexts_df = pl.read_parquet(synth_paths.contexts)

    real_candidates_lf = pl.scan_parquet(str(data_dir.candidates_dir / "*.parquet"))
    synthetic_candidates_lf = pl.scan_parquet(str(synth_paths.candidates_dir / "*.parquet"))

    real_sample_ids = pl.Series(
        "user_id", contexts_module.sample_users_for_selectivity(n_users_real, seed)
    ).cast(pl.Int32)
    synth_sample_ids = pl.Series(
        "user_id", contexts_module.sample_users_for_selectivity(scale, seed)
    ).cast(pl.Int32)

    selectivity_diff = (
        real_contexts_df.select(["context_id", "candidate_selectivity"])
        .rename({"candidate_selectivity": "real"})
        .join(
            synthetic_contexts_df.select(["context_id", "candidate_selectivity"]).rename(
                {"candidate_selectivity": "synthetic"}
            ),
            on="context_id",
        )
        .with_columns((pl.col("synthetic") - pl.col("real")).alias("diff"))
        .sort("context_id")
        .to_dicts()
    )

    comparison = {
        "popularity_cumulative_share": {
            "real": _popularity_cumulative_summary(real_candidates_lf),
            "synthetic": _popularity_cumulative_summary(synthetic_candidates_lf),
        },
        "candidate_selectivity_by_context": selectivity_diff,
        "mean_distinct_contexts_per_user": {
            "real": _mean_distinct_contexts_per_user(
                real_candidates_lf, real_contexts_df, combination_membership, real_sample_ids
            ),
            "synthetic": _mean_distinct_contexts_per_user(
                synthetic_candidates_lf, synthetic_contexts_df, combination_membership, synth_sample_ids
            ),
        },
    }

    stats.setdefault("synthetic", {}).setdefault(_synthetic_stage_key(scale), {})[
        "real_vs_synthetic_comparison"
    ] = comparison
    update_stats(data_dir.stats, "synthetic", stats["synthetic"])
    return comparison


def run_validation_scale(data_dir: DataPaths, seed: int, force: bool = False) -> dict:
    """Escala de validação obrigatória: mesma dimensão da base real (U_real,
    lido de stats.json — nunca hardcoded), para testar a fidelidade do
    gerador comparando distribuições real vs. sintética."""
    ingest_stats = read_stats(data_dir.stats)["ingest"]
    u_real = ingest_stats["U"]
    build_scale(data_dir, u_real, config.SYNTHETIC_INJECTION_FRACTION, seed, force)
    return compare_real_vs_synthetic(data_dir, u_real, seed)


def run(data_dir: DataPaths, scale: int, p: float, seed: int, force: bool) -> dict:
    return build_scale(data_dir, scale, p, seed, force)
