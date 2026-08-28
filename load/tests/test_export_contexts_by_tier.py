from __future__ import annotations

import polars as pl

from load.export_contexts_by_tier import build_tier_mapping


def _contexts_df(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={"context_id": pl.Int16, "tier": pl.Utf8, "candidate_selectivity": pl.Float64},
    )


def test_build_tier_mapping_keeps_only_the_three_selectivity_tiers():
    df = _contexts_df(
        [
            {"context_id": 0, "tier": "high", "candidate_selectivity": 0.6},
            {"context_id": 1, "tier": "medium", "candidate_selectivity": 0.2},
            {"context_id": 2, "tier": "low", "candidate_selectivity": 0.02},
            {"context_id": 3, "tier": "unused", "candidate_selectivity": 0.9},
        ]
    )
    mapping = build_tier_mapping(df)
    assert set(mapping) == {"high", "medium", "low"}
    assert mapping["low"] == {"context_id": 2, "candidate_selectivity": 0.02}


def test_build_tier_mapping_empty_without_tiered_contexts():
    df = _contexts_df([{"context_id": 0, "tier": "unused", "candidate_selectivity": 0.5}])
    assert build_tier_mapping(df) == {}
