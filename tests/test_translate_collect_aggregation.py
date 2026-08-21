"""WP-C3: collect() aggregation, including DISTINCT, slice, mixing, and multiples.

Cypher ``collect(x)`` gathers grouped values into a list. The transpiler lowers it
to an AQL ``COLLECT … AGGREGATE var = PUSH(x)`` part; ``collect(DISTINCT x)`` uses
``UNIQUE``; a trailing slice (``collect(x)[0..5]``) becomes a post-COLLECT ``LET``
over a PUSH temp; collect coexists with other AGGREGATE aggregates (count/sum/…);
and — since AQL allows many AGGREGATE parts — multiple collect() in one projection
are supported.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(scope="module")
def bundle():
    return mapping_bundle_for("finreflectkg")


def test_plain_collect_uses_into_projection(bundle) -> None:
    out = translate(
        "MATCH (o:ORG)-[:Operates_In]->(g:GPE) WITH o, collect(g.name) AS names RETURN o.name, names",
        mapping=bundle,
    )
    assert "COLLECT o_1 = o INTO _collect_rows = {names: g.name}" in out.aql
    assert "LET names = _collect_rows[*].names" in out.aql
    assert "UNIQUE(" not in out.aql  # no DISTINCT


def test_collect_distinct_wraps_unique(bundle) -> None:
    out = translate(
        "MATCH (o:ORG)-[:Operates_In]->(g:GPE) WITH o, collect(DISTINCT g.name) AS names "
        "RETURN o.name, names",
        mapping=bundle,
    )
    assert "LET names = UNIQUE(_collect_rows[*].names)" in out.aql


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
    # collect() and count() coexist in one COLLECT ... AGGREGATE ...
    out = translate(
        "MATCH (o:ORG)-[:Operates_In]->(g:GPE) "
        "WITH o, count(DISTINCT g) AS c, collect(DISTINCT g.name)[0..3] AS names "
        "RETURN o.name, c, names",
        mapping=bundle,
    )
    assert "INTO _collect_rows" in out.aql
    assert "LENGTH(UNIQUE(FOR _agg_row IN _collect_rows" in out.aql
    assert "SLICE(" in out.aql


def test_collect_in_return(bundle) -> None:
    out = translate(
        "MATCH (o:ORG)-[:Operates_In]->(g:GPE) RETURN o.name AS org, collect(DISTINCT g.name) AS names",
        mapping=bundle,
    )
    assert "INTO _collect_rows = {names: g.name}" in out.aql
    assert "LET names = UNIQUE(_collect_rows[*].names)" in out.aql


def test_two_collects_supported(bundle) -> None:
    # Multiple collect() share one INTO projection, then derive their lists.
    out = translate(
        "MATCH (o:ORG)-[:Operates_In]->(g:GPE) "
        "WITH o, collect(g.name) AS a, collect(g.type) AS b RETURN o.name, a, b",
        mapping=bundle,
    )
    assert "INTO _collect_rows = {a: g.name, b: g.type}" in out.aql
    assert "LET a = _collect_rows[*].a" in out.aql
    assert "LET b = _collect_rows[*].b" in out.aql


def test_collect_compiles_nested_comparison_expression(bundle) -> None:
    out = translate(
        "UNWIND [true, false] AS a "
        "UNWIND [true, false] AS b "
        "WITH collect((a OR b) = (a OR b)) AS equalities "
        "RETURN equalities",
        mapping=bundle,
    )

    assert "INTO _collect_rows = {equalities:" in out.aql
    assert "==" in out.aql
    assert "PUSH((a OR b) = (a OR b))" not in out.aql
