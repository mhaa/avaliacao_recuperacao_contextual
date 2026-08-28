"""Registro central que resolve as strings de cells/*.yaml (`strategy`,
`storage`) para as classes reais. Fonte única de verdade — antes desta
etapa, a lista de células viáveis vivia hardcoded em
`tests/acceptance/test_harness_all_cells.py` (cujo próprio docstring já
avisava: "trocar por carregamento de cells/*.yaml... sem mudar o resto
deste teste"), e `core/config.py` documentava que a montagem real "é
responsabilidade de service/main.py, ainda não implementado".
"""

from __future__ import annotations

import os

from core.config import CellConfig
from storage.base import StorageAdapter
from storage.opensearch import OpenSearchAdapter
from storage.postgres import PostgresAdapter
from storage.scylla import ScyllaAdapter
from storage.valkey import ValkeyAdapter
from strategies.base import Strategy
from strategies.e1_app_filter import E1AppFilter
from strategies.e2_pushdown import E2Pushdown
from strategies.e3_prematerialized import E3Prematerialized
from strategies.e4_intersection import E4Intersection

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    "e1_app_filter": E1AppFilter,
    "e2_pushdown": E2Pushdown,
    "e3_prematerialized": E3Prematerialized,
    "e4_intersection": E4Intersection,
}

STORAGE_REGISTRY: dict[str, type[StorageAdapter]] = {
    "postgres": PostgresAdapter,
    "valkey": ValkeyAdapter,
    "scylla": ScyllaAdapter,
    "opensearch": OpenSearchAdapter,
}


class MissingCredential(Exception):
    """Levantado quando uma variável de ambiente de credencial obrigatória
    não está definida — falha clara na montagem da célula, nunca uma
    tentativa de conexão com valor vazio/None."""

    def __init__(self, env_var: str, storage_name: str):
        super().__init__(
            f"variável de ambiente '{env_var}' não definida (obrigatória para '{storage_name}')"
        )
        self.env_var = env_var
        self.storage_name = storage_name


def _require_env(name: str, storage_name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise MissingCredential(name, storage_name)
    return value


def build_strategy(config: CellConfig) -> Strategy:
    return STRATEGY_REGISTRY[config.strategy]()


def build_storage(config: CellConfig) -> StorageAdapter:
    """Monta o adaptador certo a partir de `config.storage_config` (só
    host/port — nunca credenciais, que vêm do ambiente, nunca do YAML
    versionado). Cada backend tem uma forma de construtor diferente; este
    é o único lugar que precisa saber disso.
    """
    adapter_cls = STORAGE_REGISTRY[config.storage]
    # STORAGE_HOST/STORAGE_PORT sobrescrevem cells/<id>.yaml quando
    # definidos — cells/*.yaml hardcoda nomes de serviço do
    # docker-compose (ex.: "postgres"), que não resolvem numa VM na
    # nuvem; infra/modules/service/main.tf já define STORAGE_HOST com o
    # IP interno real. Ausente localmente, cai no valor do YAML de sempre.
    host = os.environ.get("STORAGE_HOST", config.storage_config["host"])
    port = int(os.environ.get("STORAGE_PORT", config.storage_config["port"]))

    if config.storage == "postgres":
        user = _require_env("POSTGRES_USER", "postgres")
        password = _require_env("POSTGRES_PASSWORD", "postgres")
        dbname = _require_env("POSTGRES_DB", "postgres")
        conninfo = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
        return adapter_cls(conninfo)

    if config.storage == "valkey":
        password = os.environ.get("VALKEY_PASSWORD")
        auth = f":{password}@" if password else ""
        url = f"redis://{auth}{host}:{port}/0"
        return adapter_cls(url)

    if config.storage == "scylla":
        return adapter_cls([str(host)])

    if config.storage == "opensearch":
        return adapter_cls([f"http://{host}:{port}"])

    raise ValueError(f"storage desconhecido: {config.storage!r}")
