from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_query_core import CoreError
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.mark.parametrize(
    "cypher,code,msg_sub",
    [
        # MultiPartQuery / WITH guardrails
        (
            "WITH 1 AS x RETURN x",
            "UNSUPPORTED",
            "MATCH is required before WITH",
        ),
        (
            "MATCH (n:User)\nWITH n\nSET n.x = 1\nRETURN n",
            "UNSUPPORTED",
            "Updating clauses are not supported",
        ),
        (
            "MATCH (n:User)\nWITH n\nUNWIND [1] AS x\nRETURN n",
            "NOT_IMPLEMENTED",
            "Only MATCH is supported after WITH",
        ),
        # Relationship detail/type guardrails
        (
            "MATCH (u:User)-->(v:User) RETURN u",
            "UNSUPPORTED",
            "Relationship detail",
        ),
        (
            "MATCH (u:User)-[]->(v:User) RETURN u",
            "UNSUPPORTED",
            "Relationship type is required",
        ),
        (
            "MATCH (u:User)-[:FOLLOWS|LIKES]->(v:User) RETURN u",
            "UNSUPPORTED",
            "Exactly one relationship type",
        ),
        # Expression compiler unsupported shapes
        (
            "MATCH (n:User) WHERE n.id = $1 RETURN n",
            "UNSUPPORTED",
            "Positional parameters not supported",
        ),
        (
            "MATCH (n:User) WHERE unknownFunc(n.age) = 1 RETURN n",
            "UNSUPPORTED",
            "Unsupported function",
        ),
        (
            "MATCH (n:User) WHERE size([1,2], [3]) = 2 RETURN n",
            "UNSUPPORTED",
            "size expects 1 arg",
        ),
        (
            "MATCH (n:User) RETURN toUpper(n.city, n.name)",
            "UNSUPPORTED",
            "toUpper expects 1 arg",
        ),
        (
            "MATCH (n:User) RETURN toLower(n.city, n.name)",
            "UNSUPPORTED",
            "toLower expects 1 arg",
        ),
        (
            'MATCH (n:User) WHERE arango.foo(n.city) = "x" RETURN n',
            "EXTENSIONS_DISABLED",
            "arango.* extension",
        ),
        # Inline node properties guardrails
        (
            "MATCH (n:User $props) RETURN n",
            "NOT_IMPLEMENTED",
            "Parameterized node properties are not supported",
        ),
        # Multi-pattern MATCH fail-fast branches
        (
            "MATCH (u:User)-[r:FOLLOWS]->(v:User), (x:User)-[r:FOLLOWS]->(y:User) RETURN u",
            "NOT_IMPLEMENTED",
            "Shared relationship variables",
        ),
        (
            "MATCH (u:User), (u)-[:FOLLOWS]->(v) RETURN u",
            "UNSUPPORTED",
            "single label is required",
        ),
    ],
)
def test_translate_fail_fast_errors_extra(cypher: str, code: str, msg_sub: str):
    mapping = mapping_bundle_for("pg")
    with pytest.raises(CoreError) as e:
        translate(cypher, mapping=mapping)
    assert e.value.code == code
    assert msg_sub.lower() in str(e.value).lower()
