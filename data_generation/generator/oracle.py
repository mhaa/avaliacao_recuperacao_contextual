"""Etapa 5 — arnês de correção (oráculo): 1000 casos de teste com resultado
esperado computado de forma independente e ingênua sobre candidates.parquet.

Não usa inverted_lists.parquet nem prematerialized.parquet como fonte da
verdade — esses são artefatos de E-3/E-4, as próprias estratégias que serão
testadas contra este oráculo. A pertença item→contexto é recalculada aqui a
partir dos CSVs brutos (mesma lógica de parsing de contexts.py, mas
recomputada, nunca lida dos artefatos derivados).
"""

from __future__ import annotations

import numpy as np
import polars as pl

from . import config, contexts as contexts_module, ingest
from .io_utils import update_stats, write_parquet_deterministic
from .paths import DataPaths, read_stats, stage_is_done
from .seeds import derive_seeds

_CONTEXT_MODES = ("single", "composed")
_EXCLUDE_MODES = ("empty", "partial20", "forcing")
_FREQS = ("high", "low")


def classify_user_frequency(ratings_df: pl.DataFrame) -> tuple[list[int], list[int]]:
    """Proxy de atividade = contagem de ratings por usuário (não há log de
    acesso na geração). Quartis superior/inferior."""
    # group_by não garante ordem de emissão dos grupos; ordenar explicitamente
    # é necessário porque a ordem de high_users/low_users alimenta rng.choice
    # mais adiante e precisa ser reprodutível entre execuções.
    counts = ratings_df.group_by("user_id").agg(pl.len().alias("n_ratings")).sort("user_id")
    q_low = counts["n_ratings"].quantile(0.25)
    q_high = counts["n_ratings"].quantile(0.75)
    high_users = counts.filter(pl.col("n_ratings") >= q_high)["user_id"].to_list()
    low_users = counts.filter(pl.col("n_ratings") <= q_low)["user_id"].to_list()
    all_users = counts["user_id"].to_list()
    return (high_users or all_users, low_users or all_users)


def build_item_context_membership(
    contexts_df: pl.DataFrame, combination_membership: pl.DataFrame
) -> dict[int, set[int]]:
    pairs = contexts_module.context_item_pairs(contexts_df, combination_membership)
    return {key[0]: set(sub["item_id"].to_list()) for key, sub in pairs.group_by("context_id")}


def sample_cases(
    rng: np.random.Generator,
    high_users: list[int],
    low_users: list[int],
    tier_context_ids: dict[str, int],
    all_context_ids: list[int],
    k: int,
    n_cases: int,
) -> list[dict]:
    """Monta os esqueletos dos n_cases casos (usuário, contexto(s), modo de
    exclusão) cobrindo: os 3 patamares, usuários de alta/baixa frequência,
    contexto único vs. composto (interseção), 3 modos de exclusão."""
    combos = [
        (cm, em, fr) for cm in _CONTEXT_MODES for em in _EXCLUDE_MODES for fr in _FREQS
    ]
    base = n_cases // len(combos)
    counts = [base] * len(combos)
    for i in range(n_cases - base * len(combos)):
        counts[i] += 1

    tier_cycle = list(tier_context_ids.values())
    tier_coverage_target = max(len(tier_cycle) * 20, 1)

    cases = []
    case_id = 0
    tier_forced = 0
    for (context_mode, exclude_mode, freq), count in zip(combos, counts):
        users_pool = high_users if freq == "high" else low_users
        for _ in range(count):
            user_id = int(rng.choice(users_pool))

            if context_mode == "single":
                if tier_cycle and tier_forced < tier_coverage_target:
                    context_ids = [tier_cycle[tier_forced % len(tier_cycle)]]
                    tier_forced += 1
                else:
                    context_ids = [int(rng.choice(all_context_ids))]
            else:
                size = min(2, len(all_context_ids))
                context_ids = sorted(
                    int(x) for x in rng.choice(all_context_ids, size=size, replace=False)
                )

            cases.append(
                {
                    "case_id": case_id,
                    "user_id": user_id,
                    "context_ids": context_ids,
                    "exclude_mode": exclude_mode,
                    "k": k,
                }
            )
            case_id += 1
    return cases


def _filtered_item_ids_by_rank(
    user_candidates: pl.DataFrame, item_context_membership: dict[int, set[int]], context_ids: list[int]
) -> list[int]:
    matching = None
    for cid in context_ids:
        items = item_context_membership.get(cid, set())
        matching = items if matching is None else (matching & items)
    df = user_candidates
    if matching is not None:
        df = df.filter(pl.col("item_id").is_in(list(matching)))
    return df.sort("rank")["item_id"].to_list()


def _resolve_exclude_ids(
    exclude_mode: str,
    user_candidates: pl.DataFrame,
    filtered_ids: list[int],
    k: int,
    rng: np.random.Generator,
) -> list[int]:
    if exclude_mode == "empty":
        return []
    if exclude_mode == "partial20":
        pool = user_candidates["item_id"].to_list()
        n_pick = min(20, len(pool))
        if n_pick == 0:
            return []
        return sorted(int(x) for x in rng.choice(pool, size=n_pick, replace=False))
    # forcing: garante resultado final com menos de k itens.
    if not filtered_ids:
        return []
    remaining = int(rng.integers(0, k))
    return sorted(filtered_ids[remaining:])


def compute_expected(
    user_candidates: pl.DataFrame,
    item_context_membership: dict[int, set[int]],
    context_ids: list[int],
    exclude_ids: list[int],
    k: int,
) -> tuple[list[int], list[float]]:
    """Referência independente e ingênua: filtra candidates em memória,
    interseção AND dos contextos, exclui exclude_ids, ordena por `rank`
    (que já encodifica score desc / item_id asc), corta em k."""
    df = user_candidates
    if context_ids:
        matching = None
        for cid in context_ids:
            items = item_context_membership.get(cid, set())
            matching = items if matching is None else (matching & items)
        df = df.filter(pl.col("item_id").is_in(list(matching)))
    if exclude_ids:
        df = df.filter(~pl.col("item_id").is_in(exclude_ids))
    ordered = df.sort("rank").head(k)
    return ordered["item_id"].to_list(), ordered["score"].to_list()


def write_oracle(cases: list[dict], path) -> None:
    df = pl.DataFrame(
        cases,
        schema={
            "case_id": pl.Int32,
            "user_id": pl.Int32,
            "context_ids": pl.List(pl.Int16),
            "exclude_ids": pl.List(pl.Int32),
            "k": pl.Int16,
            "expected_item_ids": pl.List(pl.Int32),
            "expected_scores": pl.List(pl.Float32),
        },
    )
    write_parquet_deterministic(df, path, sort_by=["case_id"])


def run(data_dir: DataPaths, seed: int, force: bool) -> dict:
    # `sample_users` não é parâmetro desta etapa (o oráculo não restringe
    # usuários por conta própria, só lê o que ingest já produziu) — mas o
    # RESULTADO depende inteiramente da escala de `ingest` (candidates.parquet,
    # id_maps.parquet). Sem incluir isso aqui, dois `all` com o mesmo seed em
    # escalas diferentes (ex.: dev depois real) marcavam a etapa como "já
    # feita" e nunca regeneravam — confirmado na prática: rodar `all` sem
    # `--sample-users` depois de já ter rodado com `--sample-users 10000`
    # manteve o oracle.parquet antigo, agora referenciando IDs densos de uma
    # população de usuários completamente diferente da que id_maps.parquet
    # passou a descrever.
    ingest_u = read_stats(data_dir.stats).get("ingest", {}).get("U")
    params = {"seed": seed, "ingest_u": ingest_u}
    if not force and stage_is_done("oracle", [data_dir.oracle], data_dir.stats, params):
        return read_stats(data_dir.stats).get("oracle", {})

    id_maps = pl.read_parquet(data_dir.id_maps)
    movies_lf = ingest.read_movies_raw(data_dir.raw_dir)

    genre_membership = contexts_module.parse_genres(movies_lf, id_maps)
    combination_membership = contexts_module.build_combination_membership(
        genre_membership, config.MAX_GENRE_COMBINATION_SIZE
    )
    contexts_df = pl.read_parquet(data_dir.contexts)
    item_context_membership = build_item_context_membership(contexts_df, combination_membership)
    tier_context_ids = {
        row["tier"]: row["context_id"]
        for row in contexts_df.filter(pl.col("tier") != "unused").iter_rows(named=True)
    }
    all_context_ids = contexts_df["context_id"].to_list()

    ratings_df = ingest.load_dense_ratings(data_dir.raw_dir, data_dir.id_maps).collect()
    high_users, low_users = classify_user_frequency(ratings_df)

    case_seeds = derive_seeds(seed)
    selection_rng = np.random.default_rng(case_seeds["oracle_case_selection"])
    exclude_rng = np.random.default_rng(case_seeds["oracle_exclude_sampling"])

    skeletons = sample_cases(
        selection_rng,
        high_users,
        low_users,
        tier_context_ids,
        all_context_ids,
        config.K_DEFAULT,
        config.ORACLE_CASE_COUNT,
    )

    needed_user_ids = sorted({c["user_id"] for c in skeletons})
    candidates_lf = pl.scan_parquet(str(data_dir.candidates_dir / "*.parquet"))
    # Ordenar explicitamente antes do group_by: group_by não garante a ordem
    # das linhas dentro de cada grupo, e essa ordem alimenta rng.choice em
    # _resolve_exclude_ids (modo "partial20") — precisa ser reprodutível.
    candidates_subset = (
        candidates_lf.filter(pl.col("user_id").is_in(needed_user_ids))
        .collect()
        .sort(["user_id", "rank"])
    )
    user_candidates = {
        key[0]: sub for key, sub in candidates_subset.group_by("user_id", maintain_order=True)
    }

    cases = []
    short_result_count = 0
    for skeleton in skeletons:
        user_id = skeleton["user_id"]
        context_ids = skeleton["context_ids"]
        k = skeleton["k"]
        user_df = user_candidates.get(user_id)
        if user_df is None:
            user_df = candidates_subset.clear()

        filtered_ids = _filtered_item_ids_by_rank(user_df, item_context_membership, context_ids)
        exclude_ids = _resolve_exclude_ids(
            skeleton["exclude_mode"], user_df, filtered_ids, k, exclude_rng
        )
        expected_item_ids, expected_scores = compute_expected(
            user_df, item_context_membership, context_ids, exclude_ids, k
        )
        if len(expected_item_ids) < k:
            short_result_count += 1

        cases.append(
            {
                "case_id": skeleton["case_id"],
                "user_id": user_id,
                "context_ids": context_ids,
                "exclude_ids": exclude_ids,
                "k": k,
                "expected_item_ids": expected_item_ids,
                "expected_scores": expected_scores,
            }
        )

    write_oracle(cases, data_dir.oracle)

    stats = {
        "params": params,
        "case_count": len(cases),
        "short_result_case_count": short_result_count,
        "high_users_pool_size": len(high_users),
        "low_users_pool_size": len(low_users),
    }
    update_stats(data_dir.stats, "oracle", stats)
    return stats
