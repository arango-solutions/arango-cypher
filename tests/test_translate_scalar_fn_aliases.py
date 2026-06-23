"""WP-C1: non-canonical scalar function aliases (upper/lower).

openCypher's canonical casing helpers are ``toUpper``/``toLower``; ``upper`` and
``lower`` are common aliases that LLMs and some dialects emit. The transpiler
accepts both, mapping them to AQL ``UPPER``/``LOWER``.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(scope="module")
def bundle():
    return mapping_bundle_for("finreflectkg")


@pytest.mark.parametrize(
    "cypher,expected_fragment",
    [
        ("MATCH (o:ORG) WHERE upper(o.name) = 'CINF' RETURN o.name", "UPPER("),
        ("MATCH (o:ORG) WHERE lower(o.name) = 'cinf' RETURN o.name", "LOWER("),
        ("MATCH (o:ORG) WHERE toUpper(o.name) = 'CINF' RETURN o.name", "UPPER("),
        ("MATCH (o:ORG) WHERE toLower(o.name) = 'cinf' RETURN o.name", "LOWER("),
        ("MATCH (o:ORG) RETURN upper(o.name) AS u", "UPPER("),
        ("MATCH (o:ORG) RETURN lower(o.name) AS l", "LOWER("),
    ],
)
def test_upper_lower_aliases_compile(bundle, cypher, expected_fragment) -> None:
    out = translate(cypher, mapping=bundle)
    assert expected_fragment in out.aql


def test_upper_alias_equivalent_to_toupper(bundle) -> None:
    a = translate("MATCH (o:ORG) RETURN upper(o.name) AS u", mapping=bundle)
    b = translate("MATCH (o:ORG) RETURN toUpper(o.name) AS u", mapping=bundle)
    assert a.aql == b.aql
