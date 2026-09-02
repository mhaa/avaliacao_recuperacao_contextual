"""Ponto de entrada do serviço: monta uma célula a partir de CELL e sobe o
transporte configurado.

Uso: CELL=e1-postgres python -m service.main
"""

from __future__ import annotations

import os

from core.config import CellConfig, load_cell_config
from core.registry import build_storage, build_strategy
from storage.base import StorageAdapter
from strategies.base import Strategy, check_compatibility


def build_cell() -> tuple[CellConfig, Strategy, StorageAdapter]:
    """Usada pelo transporte gRPC (single-process). O HTTP (T-A/T-B) monta
    a célula sozinho, por worker — ver service/http_app.py."""
    cell_id = os.environ["CELL"]
    config = load_cell_config(cell_id)
    strategy = build_strategy(config)
    storage = build_storage(config)
    check_compatibility(strategy, storage)
    return config, strategy, storage


def main() -> None:
    transport = load_cell_config(os.environ["CELL"]).transport
    if transport == "grpc":
        _, strategy, storage = build_cell()
        from service.grpc_app import serve

        serve(strategy, storage)
    else:
        # T-A e T-B são o mesmo processo Hypercorn — ver service/http_app.py.
        # Multi-worker: cada worker se monta sozinho a partir de CELL, não
        # reaproveita strategy/storage construídos aqui.
        from service.http_app import serve

        serve()


if __name__ == "__main__":
    main()
