"""Parâmetros fixos do experimento. Referenciar por nome; nunca hardcode em outro módulo."""

from __future__ import annotations

# Parâmetros fixos do experimento (docs/DESIGN.md).
N_CANDIDATES = 500
K_DEFAULT = 20
C_CONTEXTS = 20

# M > K: exclusão de sessão é aplicada em tempo de requisição sobre o
# resultado lido, então a pré-materialização precisa de folga sobre K_DEFAULT.
M_PREMATERIALIZED = 40

# Contextos são combinações de 1 ou 2 gêneros do MovieLens (interseção AND).
# O ml-32m real não inclui tag genome (só ml-20m/ml-25m tinham) nem viabiliza
# um vocabulário de etiquetas confiável a partir de tags.csv (texto livre,
# sem score de relevância) — combinações de gênero dão a granularidade de
# seletividade necessária sem depender de dado que não existe no dataset.
MAX_GENRE_COMBINATION_SIZE = 2

# Alvos de seletividade de candidatos por patamar de contexto e tolerância
# aceita entre o medido e o alvo (critério de aceitação 5).
TIER_TARGETS: dict[str, float] = {"high": 0.02, "medium": 0.20, "low": 0.60}
TIER_TOLERANCE_PP = 0.05

# ALS (Hu et al., 2008) — não são variáveis do estudo, não ajustar.
ALS_FACTORS = 64
ALS_REGULARIZATION = 0.01
ALS_ITERATIONS = 15
ALS_CONFIDENCE_ALPHA = 40  # confidence = 1 + ALS_CONFIDENCE_ALPHA * rating

DEFAULT_SEED = 42

# Tamanho do bloco de usuários para escrita particionada de candidates.parquet.
CANDIDATES_BLOCK_SIZE = 100_000

ORACLE_CASE_COUNT = 1000
ORACLE_MIN_SHORT_RESULT_CASES = 50

SYNTHETIC_SCALES = [1_000_000, 3_000_000]
SYNTHETIC_INJECTION_FRACTION = 0.3

ML32M_URL = "https://files.grouplens.org/datasets/movielens/ml-32m.zip"
ML32M_ZIP_ROOT = "ml-32m"  # diretório dentro do zip

PARQUET_COMPRESSION = "zstd"
