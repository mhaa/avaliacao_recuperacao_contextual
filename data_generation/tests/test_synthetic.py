import numpy as np
import polars as pl

from generator import artifacts, config, contexts, ingest, rank, synthetic
from generator.paths import read_stats


def test_inject_popularity_noise_preserves_size_no_dupes_and_relative_order():
    profile = pl.DataFrame(
        {
            "item_id": [10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
            "rank": list(range(1, 11)),
            "score": [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0],
        }
    )
    popularity_df = pl.DataFrame(
        {"item_id": list(range(1000, 1050)), "count": list(range(50, 0, -1))}
    )
    rng = np.random.default_rng(0)

    result = synthetic.inject_popularity_noise(profile, p=0.3, popularity_df=popularity_df, rng=rng)

    assert result.height == 10
    assert result["item_id"].n_unique() == 10
    assert result["rank"].to_list() == list(range(1, 11))
    scores = result["score"].to_list()
    assert scores == sorted(scores, reverse=True)

    kept_original_items = [i for i in profile["item_id"].to_list() if i in set(result["item_id"].to_list())]
    kept_in_result = [i for i in result.sort("rank")["item_id"].to_list() if i in set(kept_original_items)]
    assert kept_in_result == kept_original_items  # ordem relativa preservada


def test_inject_popularity_noise_with_zero_fraction_is_identity():
    profile = pl.DataFrame(
        {"item_id": [1, 2, 3], "rank": [1, 2, 3], "score": [3.0, 2.0, 1.0]}
    )
    popularity_df = pl.DataFrame({"item_id": [4, 5], "count": [1, 1]})
    rng = np.random.default_rng(0)

    result = synthetic.inject_popularity_noise(profile, p=0.0, popularity_df=popularity_df, rng=rng)

    assert result["item_id"].to_list() == [1, 2, 3]


def test_sample_synthetic_profiles_is_within_range_and_deterministic():
    real_user_ids = np.arange(10)
    a = synthetic.sample_synthetic_profiles(np.random.default_rng(7), real_user_ids, scale=100)
    b = synthetic.sample_synthetic_profiles(np.random.default_rng(7), real_user_ids, scale=100)

    assert a.tolist() == b.tolist()
    assert a.min() >= 0 and a.max() < 10
    assert len(a) == 100


def _run_real_pipeline(data_dir):
    ingest.run(data_dir, sample_users=None, seed=42, force=False)
    rank.run(data_dir, sample_users=None, seed=42, force=False)
    contexts.run(data_dir, sample_users=None, seed=42, force=False)
    artifacts.run(data_dir, sample_users=None, seed=42, force=False)


def test_build_scale_produces_correctly_sized_artifacts(rank_scale_data_dir):
    _run_real_pipeline(rank_scale_data_dir)

    scale = 50
    stats = synthetic.run(rank_scale_data_dir, scale=scale, p=0.3, seed=42, force=False)
    assert stats["n_synthetic_users"] == scale

    synth_dir = rank_scale_data_dir.synthetic_dir(scale)
    from generator.paths import DataPaths

    synth_paths = DataPaths(synth_dir)

    synth_candidates = pl.concat(
        [pl.read_parquet(f) for f in sorted(synth_paths.candidates_dir.glob("*.parquet"))]
    )
    assert synth_candidates["user_id"].n_unique() == scale
    for _, group in synth_candidates.group_by("user_id"):
        assert group.height == config.N_CANDIDATES
        assert group["item_id"].n_unique() == config.N_CANDIDATES

    synth_contexts = pl.read_parquet(synth_paths.contexts)
    real_contexts = pl.read_parquet(rank_scale_data_dir.contexts)
    assert set(synth_contexts["context_id"].to_list()) == set(real_contexts["context_id"].to_list())

    synth_prematerialized = pl.read_parquet(synth_paths.prematerialized)
    assert synth_prematerialized.height == scale * config.C_CONTEXTS


def test_build_scale_is_idempotent(rank_scale_data_dir):
    _run_real_pipeline(rank_scale_data_dir)
    stats1 = synthetic.run(rank_scale_data_dir, scale=40, p=0.3, seed=42, force=False)
    stats2 = synthetic.run(rank_scale_data_dir, scale=40, p=0.3, seed=42, force=False)
    assert stats1 == stats2


def test_run_validation_scale_uses_measured_u_real_not_hardcoded(rank_scale_data_dir):
    _run_real_pipeline(rank_scale_data_dir)
    u_real = read_stats(rank_scale_data_dir.stats)["ingest"]["U"]

    comparison = synthetic.run_validation_scale(rank_scale_data_dir, seed=42)

    assert rank_scale_data_dir.synthetic_dir(u_real).exists()
    assert "popularity_cumulative_share" in comparison
    assert "candidate_selectivity_by_context" in comparison
    assert "mean_distinct_contexts_per_user" in comparison

    stats = read_stats(rank_scale_data_dir.stats)
    assert str(u_real) in stats["synthetic"]
    assert "real_vs_synthetic_comparison" in stats["synthetic"][str(u_real)]
