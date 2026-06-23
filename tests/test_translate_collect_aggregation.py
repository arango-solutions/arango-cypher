"""WP-C3: collect() aggregation, including DISTINCT, slice, and mixing.

Cypher ``collect(x)`` gathers grouped values into a list. The transpiler maps it
to AQL ``COLLECT … INTO var = x``; ``collect(DISTINCT x)`` wraps the result in
``UNIQUE``; a trailing slice (``collect(x)[0..5]``) becomes ``SLICE``; and a
collect may now coexist with AGGREGATE aggregates (count/sum/…) in one COLLECT.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(scope="module")
def bundle():
    return mapping_bundle_for("finreflectkg")


def test_plain_collect_uses_into(bundle) -> None:
    out = translate(
        "MATCH (o:ORG)-[:Operates_In]->(g:GPE) WITH o, collect(g.name) AS names "
        "RETURN o.name, names",
        mapping=bundle,
    )
    assert "INTO" in out.aql
    assert "names = g.name" in out.aql
    assert "UNIQUE(" not in out.aql  # no DISTINCT


def test_collect_distinct_wraps_unique(bundle) -> None:
    out = translate(
        "MATCH (o:ORG)-[:Operates_In]->(g:GPE) WITH o, collect(DISTINCT g.name) AS names "
        "RETURN o.name, names",
        mapping=bundle,
    )
    assert "INTO" in out.aql
    assert "UNIQUE(" in out.aql


def test_collect_slice_uses_slice(bundle) -> None:
    out = translate(
        "MATCH (o:ORG)-[:Operates_In]->(g:GPE) "
        "WITH o, collect(DISTINCT g.name)[0..5] AS names RETURN o.name, names",
        mapping=bundle,
    )
    assert "SLICE(" in out.aql
    assert "UNIQUE(" in out.aql
    assert "(5) - (0)" in out.aql


def test_collect_mixed_with_count(bundle) -> None:
    # collect() and count() must coexist in one COLLECT ... AGGREGATE ... INTO.
    out = translate(
        "MATCH (o:ORG)-[:Operates_In]->(g:GPE) "
        "WITH o, count(DISTINCT g) AS c, collect(DISTINCT g.name)[0..3] AS names "
        "RETURN o.name, c, names",
        mapping=bundle,
    )
    assert "AGGREGATE" in out.aql
    assert "INTO" in out.aql
    assert "COUNT_DISTINCT(" in out.aql
    assert "SLICE(" in out.aql


def test_collect_in_return(bundle) -> None:
    out = translate(
        "MATCH (o:ORG)-[:Operates_In]->(g:GPE) "
        "RETURN o.name AS org, collect(DISTINCT g.name) AS names",
        mapping=bundle,
    )
    assert "INTO" in out.aql
    assert "UNIQUE(" in out.aql


def test_two_collects_rejected(bundle) -> None:
    from arango_query_core.errors import CoreError

    with pytest.raises(CoreError):
        translate(
            "MATCH (o:ORG)-[:Operates_In]->(g:GPE) "
            "WITH o, collect(g.name) AS a, collect(g.id) AS b RETURN o.name, a, b",
            mapping=bundle,
        )
