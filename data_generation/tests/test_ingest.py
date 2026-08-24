from generator.ingest import (
    build_id_maps,
    read_movies_raw,
    read_ratings_raw,
    run,
    select_sampled_users,
)


def test_id_maps_are_dense_and_contiguous(tiny_data_dir):
    ratings_lf = read_ratings_raw(tiny_data_dir.raw_dir)
    movies_lf = read_movies_raw(tiny_data_dir.raw_dir)
    user_map, item_map = build_id_maps(ratings_lf, movies_lf)

    assert sorted(user_map["dense_id"].to_list()) == list(range(user_map.height))
    assert sorted(item_map["dense_id"].to_list()) == list(range(item_map.height))
    assert user_map["original_id"].n_unique() == user_map.height
    assert item_map["original_id"].n_unique() == item_map.height


def test_sample_users_restricts_only_user_universe(tiny_data_dir):
    ratings_lf = read_ratings_raw(tiny_data_dir.raw_dir)
    movies_lf = read_movies_raw(tiny_data_dir.raw_dir)

    sampled = select_sampled_users(ratings_lf, n=10, seed=42)
    assert sampled.len() == 10

    user_map, item_map = build_id_maps(ratings_lf, movies_lf, sampled)
    assert user_map.height == 10

    full_item_map = build_id_maps(ratings_lf, movies_lf)[1]
    assert item_map.height == full_item_map.height  # universo de itens não muda


def test_select_sampled_users_is_deterministic(tiny_data_dir):
    ratings_lf = read_ratings_raw(tiny_data_dir.raw_dir)
    a = select_sampled_users(ratings_lf, n=10, seed=42).to_list()
    b = select_sampled_users(ratings_lf, n=10, seed=42).to_list()
    assert a == b


def test_run_writes_id_maps_and_stats(tiny_data_dir):
    stats = run(tiny_data_dir, sample_users=None, seed=42, force=False)

    assert tiny_data_dir.id_maps.exists()
    assert stats["U"] > 0
    assert stats["I"] > 0
    assert stats["dropped_ratings_rows"] >= 0


def test_run_is_idempotent(tiny_data_dir):
    stats1 = run(tiny_data_dir, sample_users=None, seed=42, force=False)
    mtime_before = tiny_data_dir.id_maps.stat().st_mtime

    stats2 = run(tiny_data_dir, sample_users=None, seed=42, force=False)

    assert stats2 == stats1
    assert tiny_data_dir.id_maps.stat().st_mtime == mtime_before  # não regravou


def test_run_with_different_sample_users_is_not_reused(tiny_data_dir):
    run(tiny_data_dir, sample_users=None, seed=42, force=False)
    stats_sampled = run(tiny_data_dir, sample_users=10, seed=42, force=False)

    assert stats_sampled["U"] == 10
