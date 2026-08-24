import polars as pl

from generator import config, contexts, ingest, rank


def test_parse_genres_excludes_no_genres_listed_and_assigns_dense_ids():
    movies_lf = pl.DataFrame(
        {
            "movieId": [1, 2, 3],
            "title": ["a", "b", "c"],
            "genres": ["Action|Comedy", "(no genres listed)", "Comedy"],
        }
    ).lazy()
    id_maps = pl.DataFrame(
        {"entity": ["item", "item", "item"], "original_id": [1, 2, 3], "dense_id": [0, 1, 2]}
    )

    result = contexts.parse_genres(movies_lf, id_maps)

    assert set(result["item_id"].to_list()) == {0, 2}  # item 1 (movieId=2) excluído
    assert set(result.filter(pl.col("label") == "Action")["item_id"].to_list()) == {0}
    assert set(result.filter(pl.col("label") == "Comedy")["item_id"].to_list()) == {0, 2}
    # genre_id denso por label ordenado: Action < Comedy
    action_id = result.filter(pl.col("label") == "Action")["genre_id"][0]
    comedy_id = result.filter(pl.col("label") == "Comedy")["genre_id"][0]
    assert action_id < comedy_id
    assert set(result["genre_id"].unique().to_list()) == {0, 1}


def test_build_combination_membership_includes_singles_and_cooccurring_pairs():
    genre_membership = pl.DataFrame(
        {
            "item_id": [0, 0, 1, 2],
            "genre_id": [1, 2, 1, 3],
            "label": ["Comedy", "Crime", "Comedy", "Drama"],
        }
    )

    result = contexts.build_combination_membership(genre_membership, max_size=2)

    singles = result.filter(pl.col("genre_ids").list.len() == 1)
    assert set(singles["combo_key"].to_list()) == {"1", "2", "3"}

    pairs = result.filter(pl.col("genre_ids").list.len() == 2)
    assert pairs.height == 1  # só o item 0 tem os dois gêneros (Comedy=1, Crime=2)
    assert pairs["combo_key"][0] == "1,2"
    assert pairs["item_id"][0] == 0


def test_compute_catalog_fraction_counts_distinct_items_per_combo():
    membership_df = pl.DataFrame(
        {
            "item_id": [0, 1, 2],
            "genre_ids": [[1], [1], [1, 2]],
            "combo_key": ["1", "1", "1,2"],
        }
    )

    result = contexts.compute_catalog_fraction(membership_df, n_items=4)
    by_key = dict(zip(result["combo_key"].to_list(), result["catalog_fraction"].to_list()))

    assert by_key["1"] == 2 / 4
    assert by_key["1,2"] == 1 / 4


def test_select_tier_contexts_and_c_selection_are_deterministic():
    selectivity_df = pl.DataFrame(
        {
            "combo_key": ["3", "1", "0,2", "2,4", "4"],
            "genre_ids": [[3], [1], [0, 2], [2, 4], [4]],
            "catalog_fraction": [0.5, 0.4, 0.05, 0.1, 0.02],
            "candidate_selectivity": [0.62, 0.21, 0.019, 0.2, 0.015],
        }
    )

    tier_chosen = contexts.select_tier_contexts(selectivity_df, config.TIER_TARGETS)
    assert tier_chosen["high"]["combo_key"] == "0,2"
    assert tier_chosen["medium"]["combo_key"] == "2,4"
    assert tier_chosen["low"]["combo_key"] == "3"

    selected = contexts.select_c_contexts(selectivity_df, tier_chosen, c=5)
    assert selected.height == 5
    assert set(selected["combo_key"].to_list()) == {"3", "1", "0,2", "2,4", "4"}

    genre_vocab = pl.DataFrame({"genre_id": [0, 1, 2, 3, 4], "label": ["G0", "G1", "G2", "G3", "G4"]})
    result = contexts.assign_context_ids(selected, tier_chosen, genre_vocab)

    assert result["context_id"].to_list() == [0, 1, 2, 3, 4]
    by_label = dict(zip(result["label"].to_list(), result["tier"].to_list()))
    assert by_label["G0+G2"] == "high"
    assert by_label["G2+G4"] == "medium"
    assert by_label["G3"] == "low"
    assert by_label["G1"] == "unused"
    assert by_label["G4"] == "unused"


def test_run_produces_c_contexts_and_genre_only_items_table(medium_data_dir):
    ingest.run(medium_data_dir, sample_users=None, seed=42, force=False)
    rank.run(medium_data_dir, sample_users=None, seed=42, force=False)
    stats = contexts.run(medium_data_dir, sample_users=None, seed=42, force=False)

    contexts_df = pl.read_parquet(medium_data_dir.contexts)
    assert contexts_df.height == config.C_CONTEXTS
    assert contexts_df["context_id"].to_list() == list(range(config.C_CONTEXTS))
    assert contexts_df["context_id"].n_unique() == config.C_CONTEXTS
    assert set(contexts_df.columns) == {
        "context_id",
        "genre_ids",
        "label",
        "catalog_fraction",
        "candidate_selectivity",
        "tier",
    }
    assert (contexts_df["genre_ids"].list.len() <= config.MAX_GENRE_COMBINATION_SIZE).all()
    assert (contexts_df["genre_ids"].list.len() >= 1).all()

    tiers = contexts_df["tier"].to_list()
    assert sorted(t for t in tiers if t != "unused") == ["high", "low", "medium"]

    items_df = pl.read_parquet(medium_data_dir.items)
    assert set(items_df.columns) == {"item_id", "original_movie_id", "genres"}
    assert items_df.height > 0
    assert items_df["item_id"].n_unique() == items_df.height
    assert (items_df["item_id"] >= 0).all()

    assert stats["c_contexts"]
    assert len(stats["c_contexts"]) == config.C_CONTEXTS


def test_run_is_idempotent(medium_data_dir):
    ingest.run(medium_data_dir, sample_users=None, seed=42, force=False)
    rank.run(medium_data_dir, sample_users=None, seed=42, force=False)
    stats1 = contexts.run(medium_data_dir, sample_users=None, seed=42, force=False)
    mtime_before = medium_data_dir.contexts.stat().st_mtime

    stats2 = contexts.run(medium_data_dir, sample_users=None, seed=42, force=False)

    assert stats1 == stats2
    assert medium_data_dir.contexts.stat().st_mtime == mtime_before
