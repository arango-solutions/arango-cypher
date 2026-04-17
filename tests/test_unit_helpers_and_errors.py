from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_cypher.api import TranspiledQuery
from arango_cypher.translate_v0 import translate_v0
from arango_cypher.translate_v0 import _aql_collection_ref, _compile_type_of_relationship, _pick_bind_key, _pick_fresh_var
from arango_query_core import CoreError, ExtensionPolicy, MappingBundle
from arango_query_core.aql import AqlFragment
from arango_query_core.exec import AqlExecutor
from arango_query_core.extensions import ExtensionRegistry
from arango_query_core.mapping import MappingResolver
from tests.helpers.mapping_fixtures import mapping_bundle_for


def test_transpiled_query_to_aql_query_roundtrip():
    tq = TranspiledQuery(aql="RETURN 1", bind_vars={"x": 1}, warnings=[], debug={"k": "v"})
    q = tq.to_aql_query()
    assert q.text == "RETURN 1"
    assert q.bind_vars == {"x": 1}
    assert q.debug == {"k": "v"}


@pytest.mark.parametrize(
    "cypher,mapping",
    [
        ("", mapping_bundle_for("pg")),
        ("   ", mapping_bundle_for("pg")),
        ("MATCH (n:User) RETURN n", None),
    ],
)
def test_translate_api_input_validation(cypher: str, mapping):
    with pytest.raises(CoreError) as e:
        translate(cypher, mapping=mapping)  # type: ignore[arg-type]
    assert e.value.code == "INVALID_ARGUMENT"


def test_translate_v0_requires_mapping():
    with pytest.raises(CoreError) as e:
        translate_v0("MATCH (n:User) RETURN n", mapping=None)  # type: ignore[arg-type]
    assert e.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize(
    "cypher,code,msg_sub",
    [
        ("RETURN 1", "UNSUPPORTED", "MATCH is required"),
        ("MATCH (n) RETURN n", "UNSUPPORTED", "label is required"),
        ("MATCH (u:User)-[u:FOLLOWS]->(v:User) RETURN u", "UNSUPPORTED", "Relationship variable must not shadow"),
        (
            "MATCH (u:User)-[:FOLLOWS $props]->(v:User) RETURN u",
            "NOT_IMPLEMENTED",
            "Parameterized relationship properties",
        ),
    ],
)
def test_translate_fail_fast_errors(cypher: str, code: str, msg_sub: str):
    mapping = mapping_bundle_for("pg")
    with pytest.raises(CoreError) as e:
        translate(cypher, mapping=mapping)
    assert e.value.code == code
    assert msg_sub.lower() in str(e.value).lower()


def test_aql_executor_execute_forwards_to_python_arango():
    calls: list[tuple[str, dict[str, object], int | None, dict[str, object]]] = []

    class FakeAql:
        def execute(self, text, *, bind_vars=None, batch_size=None, **kwargs):
            calls.append((text, bind_vars or {}, batch_size, kwargs))
            return [{"ok": True}]

    class FakeDb:
        aql = FakeAql()

    ex = AqlExecutor(db=FakeDb())
    out = ex.execute(
        query=TranspiledQuery(aql="RETURN 1", bind_vars={"x": 1}, warnings=[]).to_aql_query(), batch_size=123, ttl=5
    )
    assert out == [{"ok": True}]
    assert calls == [("RETURN 1", {"x": 1}, 123, {"ttl": 5})]


def test_aql_fragment_add_joins_and_merges():
    a = AqlFragment("FOR x IN 1..2", {"a": 1})
    b = AqlFragment("  RETURN x", {"b": 2})
    c = a + b
    assert c.text == "FOR x IN 1..2\nRETURN x" or c.text == "FOR x IN 1..2\n  RETURN x"
    assert c.bind_vars == {"a": 1, "b": 2}


def test_aql_fragment_add_non_fragment_returns_type_error():
    a = AqlFragment("RETURN 1")
    with pytest.raises(TypeError):
        _ = a + 1  # type: ignore[operator]


def test_aql_fragment_bind_var_collision_raises():
    a = AqlFragment("RETURN 1", {"x": 1})
    b = AqlFragment("RETURN 2", {"x": 2})
    with pytest.raises(CoreError) as e:
        _ = a + b
    assert e.value.code == "BIND_VAR_COLLISION"


def test_extension_policy_disabled():
    pol = ExtensionPolicy(enabled=False)
    with pytest.raises(CoreError) as e:
        pol.check_allowed("x")
    assert e.value.code == "EXTENSIONS_DISABLED"


def test_extension_policy_denylist():
    pol = ExtensionPolicy(enabled=True, denylist={"x"})
    with pytest.raises(CoreError) as e:
        pol.check_allowed("x")
    assert e.value.code == "EXTENSION_DENIED"


def test_extension_policy_allowlist():
    pol = ExtensionPolicy(enabled=True, allowlist={"x"})
    with pytest.raises(CoreError) as e:
        pol.check_allowed("y")
    assert e.value.code == "EXTENSION_NOT_ALLOWED"
    pol.check_allowed("x")


def test_extension_registry_unknown_function_and_procedure():
    reg = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    with pytest.raises(CoreError) as e1:
        reg.compile_function("missingFn", call_ast=None, ctx=None)
    assert e1.value.code == "UNKNOWN_EXTENSION"
    with pytest.raises(CoreError) as e2:
        reg.compile_procedure("missingProc", call_ast=None, ctx=None)
    assert e2.value.code == "UNKNOWN_EXTENSION"


def test_extension_registry_compiles_registered_function_and_procedure():
    reg = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    reg.register_function("f", lambda call_ast, ctx: ("ok_fn", call_ast, ctx))
    reg.register_procedure("p", lambda call_ast, ctx: ("ok_proc", call_ast, ctx))
    assert reg.compile_function("f", call_ast={"x": 1}, ctx={"y": 2}) == ("ok_fn", {"x": 1}, {"y": 2})
    assert reg.compile_procedure("p", call_ast={"x": 1}, ctx={"y": 2}) == ("ok_proc", {"x": 1}, {"y": 2})


def test_mapping_resolver_mapping_not_found():
    bundle = MappingBundle(conceptual_schema={}, physical_mapping={"entities": {}, "relationships": {}}, metadata={})
    r = MappingResolver(bundle)
    with pytest.raises(CoreError) as e:
        r.resolve_entity("Missing")
    assert e.value.code == "MAPPING_NOT_FOUND"
    with pytest.raises(CoreError) as e2:
        r.resolve_relationship("MISSING_REL")
    assert e2.value.code == "MAPPING_NOT_FOUND"


def test_mapping_resolver_non_dict_sections_are_treated_as_empty():
    bundle = MappingBundle(conceptual_schema={}, physical_mapping={"entities": [], "relationships": None}, metadata={})
    r = MappingResolver(bundle)
    with pytest.raises(CoreError) as e:
        r.resolve_entity("User")
    assert e.value.code == "MAPPING_NOT_FOUND"


@pytest.mark.parametrize(
    "mutate,cypher,code_contains",
    [
        (lambda pm: pm["entities"]["User"].__setitem__("style", "BOGUS"), "MATCH (n:User) RETURN n", "Unsupported entity mapping style"),
        (
            lambda pm: pm["entities"]["User"].__setitem__("collectionName", ""),
            "MATCH (u:User)-[:FOLLOWS]->(v:User) RETURN u",
            "Invalid entity mapping collectionName",
        ),
        (
            lambda pm: pm["relationships"]["FOLLOWS"].__setitem__("edgeCollectionName", ""),
            "MATCH (u:User)-[:FOLLOWS]->(v:User) RETURN u",
            "Invalid relationship mapping collection",
        ),
    ],
)
def test_translate_invalid_mapping_errors(mutate, cypher: str, code_contains: str):
    base = mapping_bundle_for("pg")
    pm = {
        "entities": {k: dict(v) for k, v in base.physical_mapping.get("entities", {}).items()},
        "relationships": {k: dict(v) for k, v in base.physical_mapping.get("relationships", {}).items()},
    }
    mutate(pm)
    bad = MappingBundle(conceptual_schema=base.conceptual_schema, physical_mapping=pm, metadata=base.metadata)
    with pytest.raises(CoreError) as e:
        translate(cypher, mapping=bad)
    assert e.value.code == "INVALID_MAPPING"
    assert code_contains.lower() in str(e.value).lower()


def test_pick_fresh_var_increments_suffix_until_free():
    forbidden = {"x", "x_1", "x_2"}
    assert _pick_fresh_var("x", forbidden_vars=forbidden) == "x_3"


def test_pick_bind_key_increments_numeric_suffix_until_free():
    bind_vars: dict[str, object] = {"@collection": "users", "@collection2": "users"}
    assert _pick_bind_key("@collection", bind_vars) == "@collection3"


def test_aql_collection_ref_requires_at_prefix():
    with pytest.raises(CoreError) as e:
        _aql_collection_ref("collection")
    assert e.value.code == "INTERNAL_ERROR"


def test_compile_type_of_relationship_generic_requires_rel_type_field():
    with pytest.raises(CoreError) as e:
        _compile_type_of_relationship("FOLLOWS", "r", "GENERIC_WITH_TYPE", bind_vars={})
    assert e.value.code == "INVALID_MAPPING"


def test_compile_type_of_relationship_generic_reads_rel_type_field():
    assert (
        _compile_type_of_relationship("FOLLOWS", "r", "GENERIC_WITH_TYPE", bind_vars={"relTypeField": "type"})
        == "r[@relTypeField]"
    )


