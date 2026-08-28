from __future__ import annotations

import pytest

from storage.base import GET_CANDIDATES, PrimitiveNotSupported
from storage.tests.fakes import FakeStorageAdapter
from strategies.base import check_compatibility
from strategies.e1_app_filter import E1AppFilter


class _MinimalFake(FakeStorageAdapter):
    name = "fake-minimal"
    supported_primitives = frozenset({GET_CANDIDATES})


class _NoPrimitivesFake(FakeStorageAdapter):
    name = "fake-empty"
    supported_primitives = frozenset()


def test_e1_assembles_with_adapter_that_only_supports_get_candidates():
    check_compatibility(E1AppFilter(), _MinimalFake())  # não deve levantar


def test_assembly_fails_when_required_primitive_missing():
    with pytest.raises(PrimitiveNotSupported) as exc_info:
        check_compatibility(E1AppFilter(), _NoPrimitivesFake())
    assert "get_candidates" in str(exc_info.value)
    assert "fake-empty" in str(exc_info.value)
