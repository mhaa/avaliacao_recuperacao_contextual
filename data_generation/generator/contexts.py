"""Etapa 3 — construção dos contextos e seleção dos C=20 contextos
materializados, com seus três patamares de seletividade.

Contextos são combinações de 1 a MAX_GENRE_COMBINATION_SIZE gêneros do
MovieLens (interseção AND) — não gênero+etiqueta como versões anteriores da
spec previam. O ml-32m real não inclui tag genome (só ml-20m/ml-25m tinham)
e tags.csv é texto livre sem score de relevância, então gêneros são a única
fonte de contexto confiável disponível no dataset.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from . import config, ingest
from .io_utils import update_stats, write_parquet_deterministic
from .paths import DataPaths, read_stats, stage_is_done


def _item_map(id_maps: pl.DataFrame) -> pl.DataFrame:
    return id_maps.filter(pl.col("entity") == "item").select(
        pl.col("original_id").alias("movieId"), pl.col("dense_id").alias("item_id")
    )


def parse_genres(movies_lf: pl.LazyFrame, id_maps: pl.DataFrame) -> pl.DataFrame:
    """item_id, genre_id (denso, por label ordenado), label. Descarta
    '(no genres listed)'."""
    item_map = _item_map(id_maps)
    exploded = (
        movies_lf.select(["movieId", "genres"])
        .with_columns(pl.col("movieId").cast(pl.Int64), pl.col("genres").str.split("|"))
        .explode("genres")
        .rename({"genres": "label"})
        .filter(pl.col("label") != "(no genres listed)")
        .collect()
    )
    labels = exploded["label"].unique().sort()
    label_to_id = {label: i for i, label in enumerate(labels)}
    exploded = exploded.with_columns(
        pl.col("label").replace_strict(label_to_id, return_dtype=pl.Int16).alias("genre_id")
    )
    return exploded.join(item_map, on="movieId", how="inner").select(
        ["item_id", "genre_id", "label"]
    )


def genre_vocabulary(genre_membership: pl.DataFrame) -> pl.DataFrame:
    return genre_membership.select(["genre_id", "label"]).unique().sort("genre_id")


def _combo_key_expr(col: str = "genre_ids") -> pl.Expr:
    return (
        pl.col(col).list.eval(pl.element().cast(pl.Utf8)).list.join(",").alias("combo_key")
    )


def build_combination_membership(genre_membership: pl.DataFrame, max_size: int) -> pl.DataFrame:
    """item_id, genre_ids (list[int16], ordenado), combo_key — combinações
    de 1..max_size gêneros que de fato ocorrem no catálogo (interseção AND).
    Só size <= 2 é suportado (pares via self-join); combos maiores ficariam
    combinatorialmente caros e, no catálogo do MovieLens, tendem a ter
    pouquíssimos itens."""
    if max_size not in (1, 2):
        raise NotImplementedError("MAX_GENRE_COMBINATION_SIZE só suporta 1 ou 2")

    singles = genre_membership.select(
        "item_id", pl.concat_list(["genre_id"]).alias("genre_ids")
    )
    parts = [singles]

    if max_size == 2:
        pairs = (
            genre_membership.join(genre_membership, on="item_id", suffix="_b")
            .filter(pl.col("genre_id") < pl.col("genre_id_b"))
            .select("item_id", pl.concat_list(["genre_id", "genre_id_b"]).alias("genre_ids"))
        )
        parts.append(pairs)

    combined = pl.concat(parts)
    return combined.with_columns(_combo_key_expr())


def compute_catalog_fraction(membership_df: pl.DataFrame, n_items: int) -> pl.DataFrame:
    return (
        membership_df.group_by("combo_key")
        .agg(
            pl.col("item_id").n_unique().alias("n_items_matching"),
            pl.col("genre_ids").first(),
        )
        .with_columns((pl.col("n_items_matching") / n_items).alias("catalog_fraction"))
        .select(["combo_key", "genre_ids", "catalog_fraction"])
    )


def sample_users_for_selectivity(
    n_users: int, seed: int, min_sample: int = 10_000
) -> np.ndarray:
    if n_users <= min_sample:
        return np.arange(n_users)
    rng = np.random.default_rng(seed)
    return np.sort(rng.choice(n_users, size=min_sample, replace=False))


def compute_membership_selectivity(
    candidates_lf: pl.LazyFrame,
    membership_df: pl.DataFrame,
    sample_user_ids: pl.Series,
) -> pl.DataFrame:
    """Fração MÉDIA (por usuário) dos candidatos que satisfaz cada contexto,
    medida sobre a amostra de usuários. Um único join+groupby vetorizado
    sobre todos os contextos candidatos de uma vez."""
    sample_ids_list = sample_user_ids.to_list()
    sampled = candidates_lf.filter(pl.col("user_id").is_in(sample_ids_list))

    totals = sampled.group_by("user_id").agg(pl.len().alias("total_candidates")).collect()

    combos = membership_df.select("combo_key").unique()

    matches = (
        sampled.join(membership_df.lazy().select(["combo_key", "item_id"]), on="item_id", how="inner")
        .group_by(["combo_key", "user_id"])
        .agg(pl.len().alias("n_matches"))
        .collect()
    )

    grid = pl.DataFrame({"user_id": sample_ids_list}, schema={"user_id": pl.Int32}).join(
        combos, how="cross"
    )
    grid = (
        grid.join(matches, on=["combo_key", "user_id"], how="left")
        .with_columns(pl.col("n_matches").fill_null(0))
        .join(totals, on="user_id", how="left")
        .with_columns((pl.col("n_matches") / pl.col("total_candidates")).alias("frac"))
    )

    return grid.group_by("combo_key").agg(pl.col("frac").mean().alias("candidate_selectivity"))


def select_tier_contexts(
    selectivity_df: pl.DataFrame, targets: dict[str, float]
) -> dict[str, dict]:
    """Argmin guloso de |candidate_selectivity - alvo| por patamar,
    high->medium->low, removendo o escolhido do pool a cada passo."""
    pool = selectivity_df
    chosen: dict[str, dict] = {}
    for tier in ("high", "medium", "low"):
        target = targets[tier]
        ranked = pool.with_columns(
            (pl.col("candidate_selectivity") - target).abs().alias("_dist")
        ).sort(["_dist", "combo_key"])  # combo_key desempata: _dist pode empatar entre execuções
        best = ranked.row(0, named=True)
        chosen[tier] = best
        pool = pool.join(
            pl.DataFrame({"combo_key": [best["combo_key"]]}), on="combo_key", how="anti"
        )
    return chosen


def select_c_contexts(
    selectivity_df: pl.DataFrame, tier_chosen: dict[str, dict], c: int
) -> pl.DataFrame:
    """Parte dos 3 contextos de patamar; completa com as combinações mais
    frequentes (catalog_fraction desc) até C."""
    chosen_keys = pl.DataFrame(
        {"combo_key": [v["combo_key"] for v in tier_chosen.values()]}
    ).unique()
    chosen_df = selectivity_df.join(chosen_keys, on="combo_key", how="inner")

    remaining_needed = c - chosen_df.height
    pool = selectivity_df.join(chosen_keys, on="combo_key", how="anti").sort(
        ["catalog_fraction", "combo_key"], descending=[True, False]
    )  # combo_key desempata: catalog_fraction pode empatar entre execuções
    fill = pool.head(remaining_needed)

    return pl.concat([chosen_df, fill]).select(
        ["combo_key", "genre_ids", "catalog_fraction", "candidate_selectivity"]
    )


def assign_context_ids(
    selected_df: pl.DataFrame, tier_chosen: dict[str, dict], genre_vocab: pl.DataFrame
) -> pl.DataFrame:
    label_map = dict(zip(genre_vocab["genre_id"].to_list(), genre_vocab["label"].to_list()))
    tier_by_key = {v["combo_key"]: tier for tier, v in tier_chosen.items()}

    ordered = selected_df.sort("combo_key")
    labels = [
        "+".join(label_map[g] for g in row["genre_ids"]) for row in ordered.iter_rows(named=True)
    ]
    tiers = [
        tier_by_key.get(row["combo_key"], "unused") for row in ordered.iter_rows(named=True)
    ]

    return ordered.with_columns(
        pl.Series("context_id", list(range(ordered.height))).cast(pl.Int16),
        pl.Series("label", labels),
        pl.Series("tier", tiers),
    ).select(
        ["context_id", "genre_ids", "label", "catalog_fraction", "candidate_selectivity", "tier"]
    )


def context_item_pairs(contexts_df: pl.DataFrame, membership_df: pl.DataFrame) -> pl.DataFrame:
    """context_id, item_id — junta os C contextos materializados à
    pertença item->combinação (calculada de novo a partir de movies.csv,
    nunca lida de um artefato derivado)."""
    return (
        contexts_df.select(["context_id", "genre_ids"])
        .with_columns(_combo_key_expr())
        .join(membership_df.select(["combo_key", "item_id"]), on="combo_key", how="inner")
        .select(["context_id", "item_id"])
    )


def build_items_table(id_maps: pl.DataFrame, genre_membership: pl.DataFrame) -> pl.DataFrame:
    item_map = id_maps.filter(pl.col("entity") == "item").select(
        pl.col("dense_id").alias("item_id"), pl.col("original_id").alias("original_movie_id")
    )
    genres_agg = genre_membership.group_by("item_id").agg(
        pl.col("genre_id").sort().alias("genres")
    )
    return (
        item_map.join(genres_agg, on="item_id", how="left")
        .with_columns(pl.col("genres").fill_null([]))
        .select(["item_id", "original_movie_id", "genres"])
    )


def run(data_dir: DataPaths, sample_users: int | None, seed: int, force: bool) -> dict:
    params = {"sample_users": sample_users, "seed": seed}
    if not force and stage_is_done(
        "contexts", [data_dir.contexts, data_dir.items], data_dir.stats, params
    ):
        return read_stats(data_dir.stats).get("contexts", {})

    ingest_stats = read_stats(data_dir.stats)["ingest"]
    n_items = ingest_stats["I"]
    n_users = ingest_stats["U"]

    id_maps = pl.read_parquet(data_dir.id_maps)
    movies_lf = ingest.read_movies_raw(data_dir.raw_dir)

    genre_membership = parse_genres(movies_lf, id_maps)
    genre_vocab = genre_vocabulary(genre_membership)
    combination_membership = build_combination_membership(
        genre_membership, config.MAX_GENRE_COMBINATION_SIZE
    )

    catalog_fraction_df = compute_catalog_fraction(combination_membership, n_items)

    sample_ids = sample_users_for_selectivity(n_users, seed)
    sample_ids_series = pl.Series("user_id", sample_ids).cast(pl.Int32)

    candidates_lf = pl.scan_parquet(str(data_dir.candidates_dir / "*.parquet"))
    candidate_selectivity_df = compute_membership_selectivity(
        candidates_lf, combination_membership, sample_ids_series
    )

    selectivity_df = catalog_fraction_df.join(
        candidate_selectivity_df, on="combo_key", how="inner"
    )

    tier_chosen = select_tier_contexts(selectivity_df, config.TIER_TARGETS)
    selected_df = select_c_contexts(selectivity_df, tier_chosen, config.C_CONTEXTS)
    contexts_df = assign_context_ids(selected_df, tier_chosen, genre_vocab)
    write_parquet_deterministic(contexts_df, data_dir.contexts, sort_by=["context_id"])

    items_df = build_items_table(id_maps, genre_membership)
    write_parquet_deterministic(items_df, data_dir.items, sort_by=["item_id"])

    tiers_report = {
        tier: {
            "genre_ids": v["genre_ids"],
            "measured_candidate_selectivity": v["candidate_selectivity"],
            "target": config.TIER_TARGETS[tier],
            "deviation_pp": abs(v["candidate_selectivity"] - config.TIER_TARGETS[tier]) * 100,
        }
        for tier, v in tier_chosen.items()
    }

    stats = {
        "params": params,
        "max_genre_combination_size": config.MAX_GENRE_COMBINATION_SIZE,
        "candidate_combinations_evaluated": selectivity_df.height,
        "tiers": tiers_report,
        "c_contexts": contexts_df.select(["context_id", "genre_ids", "label", "tier"]).to_dicts(),
    }
    update_stats(data_dir.stats, "contexts", stats)
    return stats
