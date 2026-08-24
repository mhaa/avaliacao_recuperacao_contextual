"""Download e extração do MovieLens 32M. No-op se os CSVs já existem."""

from __future__ import annotations

import shutil
import urllib.request
import zipfile
from pathlib import Path

from . import config

_RAW_FILES = (
    "ratings.csv",
    "movies.csv",
)
"""O ml-32m real não inclui genome-scores.csv/genome-tags.csv (só ml-20m/
ml-25m tinham tag genome) nem usa tags.csv — contextos vêm de combinações
de gêneros de movies.csv (ver generator/contexts.py)."""


def is_dataset_present(raw_dir: Path) -> bool:
    return all((raw_dir / name).exists() for name in _RAW_FILES)


def ensure_dataset(raw_dir: Path, url: str = config.ML32M_URL) -> Path:
    """Garante que os CSVs do ml-32m existem em raw_dir; baixa e extrai senão."""
    if is_dataset_present(raw_dir):
        return raw_dir

    raw_dir.mkdir(parents=True, exist_ok=True)
    zip_path = raw_dir.parent / "ml-32m.zip"

    if not zip_path.exists():
        urllib.request.urlretrieve(url, zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        for name in _RAW_FILES:
            member = f"{config.ML32M_ZIP_ROOT}/{name}"
            with zf.open(member) as src, (raw_dir / name).open("wb") as dst:
                shutil.copyfileobj(src, dst)

    if not is_dataset_present(raw_dir):
        raise RuntimeError(
            f"Download/extração concluídos mas arquivos esperados ausentes em {raw_dir}"
        )
    return raw_dir
