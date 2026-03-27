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
            "MATCH (n:User)\nWITH n\nMATCH (n)\nMATCH (n)\nRETURN n",
            "UNSUPPORTED",
            "Multiple MATCH clauses",
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

        # SKIP/LIMIT restrictions (only integer literal supported in v0)
        (
            "MATCH (n:User) RETURN n SKIP $s LIMIT 1",
            "UNSUPPORTED",
            "SKIP only supports integer literal",
        ),
        (
            "MATCH (n:User) RETURN n SKIP 0 LIMIT $l",
            "UNSUPPORTED",
            "LIMIT only supports integer literal",
        ),
        (
            "MATCH (n:User) RETURN n SKIP -1 LIMIT 1",
            "UNSUPPORTED",
            "SKIP only supports integer literal",
        ),
        (
            "MATCH (n:User) RETURN n SKIP 1+1 LIMIT 1",
            "UNSUPPORTED",
            "SKIP only supports integer literal",
        ),
        (
            "MATCH (n:User) RETURN n SKIP 0 LIMIT 1.5",
            "UNSUPPORTED",
            "LIMIT only supports integer literal",
        ),

        # Expression compiler unsupported shapes
        (
            "MATCH (n:User) WHERE n.age + 1 = 2 RETURN n",
            "UNSUPPORTED",
            "Arithmetic not supported",
        ),
        (
            "MATCH (n:User) WHERE -1 = 1 RETURN n",
            "UNSUPPORTED",
            "Unary +/- not supported",
        ),
        (
            "MATCH (n:User) WHERE n.name STARTS WITH \"A\" RETURN n",
            "UNSUPPORTED",
            "String operators not supported",
        ),
        (
            "MATCH (n:User) RETURN {a: 1}",
            "UNSUPPORTED",
            "Map literal not supported",
        ),
        (
            "MATCH (n:User) WHERE n.id = $1 RETURN n",
            "UNSUPPORTED",
            "Positional parameters not supported",
        ),
        (
            "MATCH (n:User) WHERE abs(n.age) = 1 RETURN n",
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
            "MATCH (n:User) WHERE arango.foo(n.city) = \"x\" RETURN n",
            "NOT_IMPLEMENTED",
            "arango.* extensions not implemented",
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

        # Multi-pattern-part fail-fast: named pattern parts + multiple labels in a node
        (
            "MATCH (u:User:Person), (v:User) RETURN u",
            "NOT_IMPLEMENTED",
            "Multi-label node patterns require",
        ),

        # Single pattern: multiple labels not allowed in v0
        (
            "MATCH (n:User:Person) RETURN n",
            "NOT_IMPLEMENTED",
            "Multi-label node patterns require",
        ),
    ],
)
def test_translate_fail_fast_errors_extra(cypher: str, code: str, msg_sub: str):
    mapping = mapping_bundle_for("pg")
    with pytest.raises(CoreError) as e:
        translate(cypher, mapping=mapping)
    assert e.value.code == code
    assert msg_sub.lower() in str(e.value).lower()

