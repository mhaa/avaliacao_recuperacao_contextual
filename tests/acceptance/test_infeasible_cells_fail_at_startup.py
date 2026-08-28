"""As células arquiteturalmente inviáveis da matriz de CONTEXTO.md devem
falhar na MONTAGEM da célula, com uma mensagem que nomeia a primitiva
ausente — nunca em tempo de requisição, e nunca silenciosamente. Ver
`storage/base.py:PrimitiveNotSupported` e
`strategies/base.py:check_compatibility`.

Passa a CLASSE do adaptador (não uma instância) para `check_compatibility`
— `supported_primitives`/`name` são atributos de classe, então a checagem
não abre nenhuma conexão: é isso que garante a falha ser rápida e não
depender do banco estar no ar.

As 2 células inviáveis da matriz 4x4 (CONTEXTO.md) estão aqui: E-4/Scylla
(sem primitiva de interseção) e E-3/OpenSearch (pré-materialização sem
sentido arquitetural sobre um índice invertido).
"""

from __future__ import annotations

import pytest

from storage.base import PrimitiveNotSupported
from storage.opensearch import OpenSearchAdapter
from storage.scylla import ScyllaAdapter
from strategies.base import check_compatibility
from strategies.e3_prematerialized import E3Prematerialized
from strategies.e4_intersection import E4Intersection


def test_e4_scylla_fails_at_startup_naming_intersect():
    with pytest.raises(PrimitiveNotSupported) as exc_info:
        check_compatibility(E4Intersection(), ScyllaAdapter)
    assert "intersect" in str(exc_info.value)
    assert "scylla" in str(exc_info.value)


def test_e3_opensearch_fails_at_startup_naming_prematerialized():
    with pytest.raises(PrimitiveNotSupported) as exc_info:
        check_compatibility(E3Prematerialized(), OpenSearchAdapter)
    assert "prematerialized" in str(exc_info.value)
    assert "opensearch" in str(exc_info.value)
