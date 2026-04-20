from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_query_core import CoreError, ExtensionPolicy
from tests.helpers.mapping_fixtures import mapping_bundle_for


def test_corpus_cases_load(corpus_cases):
    assert len(corpus_cases) >= 30


@pytest.mark.parametrize("idx", range(0, 30))
def test_translate_smoke_first_30_cases(corpus_cases, idx: int):
    case = corpus_cases[idx]
    policy = ExtensionPolicy(enabled=bool(case.extensions_enabled))

    mapping = mapping_bundle_for(case.mapping_fixture)

    # Smoke rule: translation should either succeed with non-empty AQL,
    # or fail with a structured CoreError (until we implement more cases).
    try:
        out = translate(case.cypher, mapping=mapping, extensions=policy, params=case.params)
        assert isinstance(out.aql, str) and out.aql.strip()
        assert isinstance(out.bind_vars, dict)
    except CoreError:
        pass

