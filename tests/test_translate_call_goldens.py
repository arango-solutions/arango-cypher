from __future__ import annotations

import pytest

from arango_cypher import register_all_extensions, translate
from arango_query_core import ExtensionPolicy, ExtensionRegistry
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture()
def all_registry() -> ExtensionRegistry:
    r = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    register_all_extensions(r)
    return r


@pytest.mark.parametrize("case_id", ["C228", "C229", "C230", "C231"])
def test_translate_call_goldens(corpus_cases, case_id: str, all_registry):
    case = next(c for c in corpus_cases if c.id == case_id)
    mapping = mapping_bundle_for(case.mapping_fixture)

    out = translate(
        case.cypher,
        mapping=mapping,
        registry=all_registry if case.extensions_enabled else None,
    )

    assert case.expected_aql is not None, f"Golden AQL must be filled for {case_id}"
    assert out.aql.strip() == case.expected_aql.strip()
    assert out.bind_vars == case.expected_bind_vars
