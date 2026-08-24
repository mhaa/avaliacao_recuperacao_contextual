import polars as pl

from generator import config, ingest, rank
from generator.paths import read_stats


def _read_candidates(data_dir) -> pl.DataFrame:
    files = sorted(data_dir.candidates_dir.glob("*.parquet"))
    return pl.concat([pl.read_parquet(f) for f in files])


def test_assign_ranks_tie_break_is_score_desc_then_item_id_asc():
    df = pl.DataFrame(
        {"user_id": [0, 0, 0], "item_id": [10, 5, 7], "score": [1.0, 2.0, 2.0]}
    )
    ranked = rank.assign_ranks(df).sort("rank")

    assert ranked["item_id"].to_list() == [5, 7, 10]
    assert ranked["rank"].to_list() == [1, 2, 3]


def test_rank_is_contiguous_permutation_and_item_ids_within_bounds(tiny_data_dir):
    ingest.run(tiny_data_dir, sample_users=None, seed=42, force=False)
    rank_stats = rank.run(tiny_data_dir, sample_users=None, seed=42, force=False)

    ingest_stats = read_stats(tiny_data_dir.stats)["ingest"]
    n_items = ingest_stats["I"]

    # Capacidade máxima por usuário exclui itens já avaliados (nunca
    # recomendados, nem no ALS nem no backfill) — no catálogo pequeno da
    # fixture "tiny" isso fica abaixo de N_CANDIDATES para vários usuários.
    ratings_df = ingest.load_dense_ratings(tiny_data_dir.raw_dir, tiny_data_dir.id_maps).collect()
    liked_df = ratings_df.group_by("user_id").agg(pl.len().alias("liked"))
    liked_counts = dict(zip(liked_df["user_id"].to_list(), liked_df["liked"].to_list()))

    candidates = _read_candidates(tiny_data_dir)

    assert (candidates["item_id"] >= 0).all()
    assert (candidates["item_id"] < n_items).all()

    for key, group in candidates.group_by("user_id"):
        user_id = key[0]
        liked = liked_counts.get(user_id, 0)
        expected_count = min(config.N_CANDIDATES, n_items - liked)

        ranks = sorted(group["rank"].to_list())
        assert ranks == list(range(1, expected_count + 1))
        assert group["item_id"].n_unique() == expected_count  # sem duplicatas

        rated_items = set(
            ratings_df.filter(pl.col("user_id") == user_id)["item_id"].to_list()
        )
        assert set(group["item_id"].to_list()).isdisjoint(rated_items)

    assert rank_stats["total_candidate_rows"] == candidates.height


def test_full_n_candidates_when_catalog_larger_than_n(rank_scale_data_dir):
    ingest.run(rank_scale_data_dir, sample_users=None, seed=42, force=False)
    rank.run(rank_scale_data_dir, sample_users=None, seed=42, force=False)

    candidates = _read_candidates(rank_scale_data_dir)
    for _, group in candidates.group_by("user_id"):
        assert sorted(group["rank"].to_list()) == list(range(1, config.N_CANDIDATES + 1))
        assert group["item_id"].n_unique() == config.N_CANDIDATES


def test_als_params_are_pinned_and_recorded(tiny_data_dir):
    ingest.run(tiny_data_dir, sample_users=None, seed=42, force=False)
    rank_stats = rank.run(tiny_data_dir, sample_users=None, seed=42, force=False)

    als = rank_stats["als"]
    assert als["factors"] == config.ALS_FACTORS
    assert als["regularization"] == config.ALS_REGULARIZATION
    assert als["iterations"] == config.ALS_ITERATIONS
    assert als["use_gpu"] is False
    assert als["num_threads"] == 1


def test_rank_run_is_idempotent(tiny_data_dir):
    ingest.run(tiny_data_dir, sample_users=None, seed=42, force=False)
    stats1 = rank.run(tiny_data_dir, sample_users=None, seed=42, force=False)
    block_files_before = sorted(f.stat().st_mtime for f in tiny_data_dir.candidates_dir.glob("*.parquet"))

    stats2 = rank.run(tiny_data_dir, sample_users=None, seed=42, force=False)
    block_files_after = sorted(f.stat().st_mtime for f in tiny_data_dir.candidates_dir.glob("*.parquet"))

    assert stats1 == stats2
    assert block_files_before == block_files_after
