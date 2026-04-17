from __future__ import annotations

import json

from arango_cypher import (
    PROFILE_SCHEMA_VERSION,
    get_cypher_profile,
    validate_cypher_profile,
)
from tests.helpers.mapping_fixtures import mapping_bundle_for


def test_get_cypher_profile_json_serializable():
    p = get_cypher_profile()
    json.dumps(p)
    assert p["profile_schema_version"] == PROFILE_SCHEMA_VERSION
    assert p["translator_id"] == "arango_cypher.translate_v0"
    assert "supported" in p and "not_yet_supported" in p
    assert "MATCH" in p["supported"]["reading_clauses"]


def test_validate_empty_cypher():
    r = validate_cypher_profile("   ")
    assert not r.ok
    assert r.first_error_code == "INVALID_ARGUMENT"


def test_validate_syntax_error_without_mapping():
    r = validate_cypher_profile("MATCH (n:User) RETURN )")
    assert not r.ok
    assert r.first_error_code == "CYPHER_SYNTAX_ERROR"


def test_validate_syntax_ok_without_mapping():
    r = validate_cypher_profile("MATCH (n:User) RETURN n")
    assert r.ok
    assert r.errors == ()


def test_validate_translate_failure_with_mapping():
    mapping = mapping_bundle_for("pg")
    r = validate_cypher_profile(
        "MERGE (a:Person {name: 'x'}) MERGE (b:Person {name: 'y'})",
        mapping=mapping,
    )
    assert not r.ok
    assert r.first_error_code == "NOT_IMPLEMENTED"


def test_validate_ok_with_mapping():
    mapping = mapping_bundle_for("pg")
    r = validate_cypher_profile("MATCH (n:User) RETURN n.name", mapping=mapping)
    assert r.ok


def test_validate_multiple_rel_types_rejected():
    mapping = mapping_bundle_for("pg")
    r = validate_cypher_profile(
        "MATCH (a:User)-[:FOLLOWS|KNOWS]->(b:User) RETURN a",
        mapping=mapping,
    )
    assert not r.ok
    assert r.first_error_code == "UNSUPPORTED"
