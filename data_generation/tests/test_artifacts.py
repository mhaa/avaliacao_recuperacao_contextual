import polars as pl
from pyroaring import BitMap

from generator import artifacts, config, contexts, ingest, rank
from generator.paths import read_stats


def _run_pipeline(data_dir):
    ingest.run(data_dir, sample_users=None, seed=42, force=False)
    rank.run(data_dir, sample_users=None, seed=42, force=False)
    contexts.run(data_dir, sample_users=None, seed=42, force=False)
    return artifacts.run(data_dir, sample_users=None, seed=42, force=False)


def test_prematerialized_row_count_is_exactly_u_times_c(medium_data_dir):
    stats = _run_pipeline(medium_data_dir)

    ingest_stats = read_stats(medium_data_dir.stats)["ingest"]
    expected = ingest_stats["U"] * config.C_CONTEXTS

    prematerialized = pl.read_parquet(medium_data_dir.prematerialized)
    assert prematerialized.height == expected
    assert stats["prematerialized_rows"] == expected


def test_prematerialized_lists_are_truncated_at_m_and_consistent(medium_data_dir):
    _run_pipeline(medium_data_dir)
    prematerialized = pl.read_parquet(medium_data_dir.prematerialized)

    lengths = prematerialized.with_columns(pl.col("item_ids").list.len().alias("n"))
    assert (lengths["n"] <= config.M_PREMATERIALIZED).all()
    assert (
        prematerialized["item_ids"].list.len() == prematerialized["scores"].list.len()
    ).all()


def test_inverted_lists_cover_c_contexts_sorted_ascending(medium_data_dir):
    _run_pipeline(medium_data_dir)
    inverted = pl.read_parquet(medium_data_dir.inverted_lists)

    assert inverted.height == config.C_CONTEXTS
    for row in inverted.iter_rows(named=True):
        assert row["item_ids"] == sorted(row["item_ids"])


def test_bitmap_intersection_matches_naive_filter(medium_data_dir):
    _run_pipeline(medium_data_dir)

    inverted = pl.read_parquet(medium_data_dir.inverted_lists)
    candidates = pl.concat(
        [pl.read_parquet(f) for f in sorted(medium_data_dir.candidates_dir.glob("*.parquet"))]
    )
    user_ids = candidates["user_id"].unique().to_list()[:20]

    checked = 0
    for row in inverted.iter_rows(named=True):
        context_id = row["context_id"]
        expected_items = set(row["item_ids"])
        bin_path = medium_data_dir.inverted_bitmaps_dir / f"{context_id}.bin"
        bitmap = BitMap.deserialize(bin_path.read_bytes())
        assert set(bitmap) == expected_items  # serialização não perde/altera itens

        for user_id in user_ids:
            user_items = set(
                candidates.filter(pl.col("user_id") == user_id)["item_id"].to_list()
            )
            naive = user_items & expected_items
            via_bitmap = set(bitmap & BitMap(user_items))
            assert via_bitmap == naive
            checked += 1

    assert checked >= 100


def test_artifacts_run_is_idempotent(medium_data_dir):
    stats1 = _run_pipeline(medium_data_dir)
    mtime_before = medium_data_dir.prematerialized.stat().st_mtime

    stats2 = artifacts.run(medium_data_dir, sample_users=None, seed=42, force=False)

    assert stats1 == stats2
    assert medium_data_dir.prematerialized.stat().st_mtime == mtime_before
