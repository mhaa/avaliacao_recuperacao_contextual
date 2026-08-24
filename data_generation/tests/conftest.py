"""Fixtures de dados brutos fabricados (sem rede) para testes rápidos.

O ml-32m real só traz ratings.csv/movies.csv/tags.csv/links.csv — sem tag
genome. A fixture reflete isso: só gera o que o pipeline realmente lê
(ratings.csv, movies.csv).

tiny: ~50 usuários / 100 filmes, para testes unitários de lógica de etapa.
medium: ~2000 usuários / 300 filmes, com viés suficiente de gênero para
testes que precisam de riqueza estatística (seleção de patamar, oráculo).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl
import pytest

from generator.paths import DataPaths

# 10 gêneros (subconjunto dos ~19 do MovieLens real) — combinações de 1-2
# dão 10 + 45 = 55 contextos candidatos, folga confortável acima de C=20.
GENRES = [
    "Action",
    "Adventure",
    "Animation",
    "Comedy",
    "Crime",
    "Drama",
    "Horror",
    "Romance",
    "Sci-Fi",
    "Thriller",
]


def _make_fixture(
    raw_dir: Path, n_users: int, n_movies: int, seed: int, ratings_per_user: int = 10
) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)

    # movieId esparso (como no MovieLens real), para exercitar a densificação.
    movie_ids = np.sort(
        rng.choice(np.arange(1, n_movies * 50), size=n_movies, replace=False)
    )
    user_ids = np.arange(1, n_users + 1)

    genres_per_movie = [
        "(no genres listed)"
        if rng.random() < 0.05
        else "|".join(rng.choice(GENRES, size=int(rng.integers(1, 3)), replace=False))
        for _ in movie_ids
    ]
    pl.DataFrame(
        {"movieId": movie_ids, "title": [f"Movie {i}" for i in movie_ids], "genres": genres_per_movie}
    ).write_csv(raw_dir / "movies.csv")

    ts = 1_000_000_000
    rows = []
    for u in user_ids:
        k = min(ratings_per_user, n_movies)
        rated = rng.choice(movie_ids, size=k, replace=False)
        for m in rated:
            rating = float(rng.choice(np.arange(0.5, 5.5, 0.5)))
            rows.append((int(u), int(m), rating, ts))
            ts += 1
    pl.DataFrame(rows, schema=["userId", "movieId", "rating", "timestamp"], orient="row").write_csv(
        raw_dir / "ratings.csv"
    )

    return raw_dir


@pytest.fixture
def tiny_data_dir(tmp_path: Path) -> DataPaths:
    data_dir = DataPaths(tmp_path / "data")
    # n_movies >= ALS_FACTORS (64) para o treino não ser degenerado.
    _make_fixture(data_dir.raw_dir, n_users=50, n_movies=100, seed=1, ratings_per_user=10)
    return data_dir


@pytest.fixture
def medium_data_dir(tmp_path: Path) -> DataPaths:
    data_dir = DataPaths(tmp_path / "data")
    _make_fixture(data_dir.raw_dir, n_users=2000, n_movies=300, seed=2, ratings_per_user=60)
    return data_dir


@pytest.fixture
def rank_scale_data_dir(tmp_path: Path) -> DataPaths:
    """n_movies > N_CANDIDATES (500) para exercitar rank sem exaustão de catálogo."""
    data_dir = DataPaths(tmp_path / "data")
    _make_fixture(data_dir.raw_dir, n_users=30, n_movies=520, seed=3, ratings_per_user=15)
    return data_dir
