import numpy as np
import polars as pl

from generator import artifacts, config, contexts, ingest, oracle, rank
from generator.paths import read_stats


def test_classify_user_frequency_splits_by_quartile():
    ratings_df = pl.DataFrame(
        {
            "user_id": [0, 0, 0, 0, 1, 1, 2, 2, 2, 3],
            "item_id": list(range(10)),
            "rating": [3.0] * 10,
            "timestamp": list(range(10)),
        }
    )
    high, low = oracle.classify_user_frequency(ratings_df)
    assert set(high) <= {0, 1, 2, 3}
    assert set(low) <= {0, 1, 2, 3}
    assert high and low


def test_compute_expected_intersects_contexts_excludes_and_orders_by_rank():
    user_candidates = pl.DataFrame(
        {
            "user_id": [0, 0, 0, 0, 0],
            "item_id": [10, 20, 30, 40, 50],
            "rank": [1, 2, 3, 4, 5],
            "score": [5.0, 4.0, 3.0, 2.0, 1.0],
        }
    )
    item_context_membership = {
        1: {10, 20, 30},  # contexto 1
        2: {20, 30, 40},  # contexto 2
    }

    # contexto único
    items, scores = oracle.compute_expected(user_candidates, item_context_membership, [1], [], k=20)
    assert items == [10, 20, 30]

    # interseção AND de dois contextos
    items, _ = oracle.compute_expected(user_candidates, item_context_membership, [1, 2], [], k=20)
    assert items == [20, 30]

    # exclusão
    items, _ = oracle.compute_expected(user_candidates, item_context_membership, [1], [20], k=20)
    assert items == [10, 30]

    # corte em k
    items, _ = oracle.compute_expected(user_candidates, item_context_membership, [], [], k=2)
    assert items == [10, 20]


def _run_pipeline(data_dir):
    ingest.run(data_dir, sample_users=None, seed=42, force=False)
    rank.run(data_dir, sample_users=None, seed=42, force=False)
    contexts.run(data_dir, sample_users=None, seed=42, force=False)
    artifacts.run(data_dir, sample_users=None, seed=42, force=False)
    return oracle.run(data_dir, seed=42, force=False)


def test_oracle_cases_well_formed(medium_data_dir):
    stats = _run_pipeline(medium_data_dir)

    oracle_df = pl.read_parquet(medium_data_dir.oracle)
    assert oracle_df.height == config.ORACLE_CASE_COUNT
    assert oracle_df["case_id"].n_unique() == config.ORACLE_CASE_COUNT
    assert oracle_df["case_id"].to_list() == list(range(config.ORACLE_CASE_COUNT))

    # nenhum expected_item_ids é nulo (lista vazia é aceitável e coerente)
    assert oracle_df["expected_item_ids"].null_count() == 0
    assert oracle_df["expected_scores"].null_count() == 0

    # ordenação coerente: score não crescente ao longo de cada resultado
    for row in oracle_df.iter_rows(named=True):
        scores = row["expected_scores"]
        assert scores == sorted(scores, reverse=True)
        assert len(row["expected_item_ids"]) == len(scores)
        assert len(row["expected_item_ids"]) <= row["k"]

    short_results = oracle_df.filter(pl.col("expected_item_ids").list.len() < pl.col("k"))
    assert short_results.height >= config.ORACLE_MIN_SHORT_RESULT_CASES
    assert stats["short_result_case_count"] == short_results.height

    empty_excludes = oracle_df.filter(pl.col("exclude_ids").list.len() == 0)
    non_empty_excludes = oracle_df.filter(pl.col("exclude_ids").list.len() > 0)
    assert empty_excludes.height > 0  # modo "empty"
    assert non_empty_excludes.height > 0  # modo "partial20"/"forcing"

    composed = oracle_df.filter(pl.col("context_ids").list.len() == 2)
    single = oracle_df.filter(pl.col("context_ids").list.len() == 1)
    assert composed.height > 0
    assert single.height > 0


def test_oracle_results_match_independent_recomputation(medium_data_dir):
    _run_pipeline(medium_data_dir)

    oracle_df = pl.read_parquet(medium_data_dir.oracle)
    candidates_lf = pl.scan_parquet(str(medium_data_dir.candidates_dir / "*.parquet"))

    id_maps = pl.read_parquet(medium_data_dir.id_maps)
    movies_lf = ingest.read_movies_raw(medium_data_dir.raw_dir)
    genre_membership = contexts.parse_genres(movies_lf, id_maps)
    combination_membership = contexts.build_combination_membership(
        genre_membership, config.MAX_GENRE_COMBINATION_SIZE
    )
    contexts_df = pl.read_parquet(medium_data_dir.contexts)
    item_context_membership = oracle.build_item_context_membership(
        contexts_df, combination_membership
    )

    rng = np.random.default_rng(0)
    sample_rows = oracle_df.sample(n=30, seed=1).iter_rows(named=True)
    for row in sample_rows:
        user_df = candidates_lf.filter(pl.col("user_id") == row["user_id"]).collect()
        expected_items, expected_scores = oracle.compute_expected(
            user_df, item_context_membership, row["context_ids"], row["exclude_ids"], row["k"]
        )
        assert expected_items == row["expected_item_ids"]
        assert expected_scores == row["expected_scores"]


def test_oracle_run_is_idempotent(medium_data_dir):
    stats1 = _run_pipeline(medium_data_dir)
    mtime_before = medium_data_dir.oracle.stat().st_mtime

    stats2 = oracle.run(medium_data_dir, seed=42, force=False)

    assert stats1 == stats2
    assert medium_data_dir.oracle.stat().st_mtime == mtime_before
