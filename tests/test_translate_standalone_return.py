"""Standalone ``RETURN`` (no MATCH/UNWIND/CALL) translation.

A query that is only a ``RETURN`` of constant/computed expressions maps to a
top-level AQL ``RETURN``. Previously the translator rejected these with
"MATCH is required in v0 subset".
"""

from __future__ import annotations

import pytest
from arango_query_core import CoreError

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture
def mapping():
    return mapping_bundle_for("movies_pg")


class TestStandaloneReturnSupported:
    def test_string_literal(self, mapping) -> None:
        out = translate('RETURN "hello"', mapping=mapping)
        assert out.aql.strip() == 'RETURN "hello"'

    def test_integer_literal(self, mapping) -> None:
        out = translate("RETURN 42", mapping=mapping)
        assert out.aql.strip() == "RETURN 42"

    def test_computed_expression_with_alias(self, mapping) -> None:
        out = translate("RETURN 1 + 1 AS x", mapping=mapping)
        assert out.aql.strip() == "RETURN {x: (1 + 1)}"

    def test_multiple_aliased_items_become_object(self, mapping) -> None:
        out = translate('RETURN "hi" AS a, 2 AS b', mapping=mapping)
        assert out.aql.strip() == 'RETURN {a: "hi", b: 2}'

    def test_boolean_literal(self, mapping) -> None:
        out = translate("RETURN true", mapping=mapping)
        assert out.aql.strip() == "RETURN true"


class TestStandaloneReturnRejected:
    @pytest.mark.parametrize(
        "cypher",
        [
            "RETURN *",
            "RETURN DISTINCT 1",
            "RETURN 1 ORDER BY 1",
            "RETURN 1 LIMIT 1",
            "RETURN 1 SKIP 1",
            "RETURN count(*)",
        ],
    )
    def test_stream_only_constructs_rejected(self, mapping, cypher: str) -> None:
        with pytest.raises(CoreError) as exc:
            translate(cypher, mapping=mapping)
        assert exc.value.code == "UNSUPPORTED"
