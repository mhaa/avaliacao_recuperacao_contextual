"""Derivação determinística de seeds por uso, a partir de uma seed mestre."""

from __future__ import annotations

import numpy as np

_USES = (
    "als",
    "sample_users",
    "synthetic_sampling",
    "synthetic_injection",
    "oracle_case_selection",
    "oracle_exclude_sampling",
)


def derive_seeds(master_seed: int) -> dict[str, int]:
    """Deriva uma seed filha por uso a partir da seed mestre.

    Usa SeedSequence.spawn para que as seeds filhas sejam estatisticamente
    independentes entre si, e determinísticas para uma dada master_seed.
    """
    root = np.random.SeedSequence(master_seed)
    children = root.spawn(len(_USES))
    return {
        use: int(child.generate_state(1, dtype=np.uint32)[0])
        for use, child in zip(_USES, children)
    }
