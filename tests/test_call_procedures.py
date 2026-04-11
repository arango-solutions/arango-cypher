"""Tests for CALL ... YIELD procedure translation and arango.* procedure extensions."""

from __future__ import annotations

import pytest
from arango_query_core import CoreError, ExtensionPolicy, ExtensionRegistry

from arango_cypher import register_all_extensions, translate
from arango_cypher.extensions.procedures import register_procedure_extensions
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture()
def proc_registry() -> ExtensionRegistry:
    r = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    register_procedure_extensions(r)
    return r


@pytest.fixture()
def all_registry() -> ExtensionRegistry:
    r = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    register_all_extensions(r)
    return r


@pytest.fixture()
def pg_mapping():
    return mapping_bundle_for("pg")


class TestStandaloneCall:
    def test_call_fulltext_yield(self, all_registry, pg_mapping):
        out = translate(
            "CALL arango.fulltext('users', 'name', 'test') YIELD doc",
            mapping=pg_mapping, registry=all_registry,
        )
        assert "FULLTEXT('users', 'name', 'test')" in out.aql
        assert "FOR doc IN" in out.aql
        assert "RETURN doc" in out.aql

    def test_call_without_yield(self, all_registry, pg_mapping):
        out = translate(
            "CALL arango.fulltext('users', 'name', 'test')",
            mapping=pg_mapping, registry=all_registry,
        )
        assert "FULLTEXT(" in out.aql
        assert "RETURN _call_row" in out.aql

    def test_call_near(self, all_registry, pg_mapping):
        out = translate(
            "CALL arango.near('places', 40.7, -74.0) YIELD loc",
            mapping=pg_mapping, registry=all_registry,
        )
        assert "NEAR('places', 40.7, (-74.0))" in out.aql
        assert "FOR loc IN" in out.aql

    def test_call_within(self, all_registry, pg_mapping):
        out = translate(
            "CALL arango.within('places', 40.7, -74.0, 1000) YIELD loc",
            mapping=pg_mapping, registry=all_registry,
        )
        assert "WITHIN('places', 40.7, (-74.0), 1000)" in out.aql

    def test_unsupported_procedure_rejected(self, pg_mapping):
        with pytest.raises(CoreError) as exc_info:
            translate(
                "CALL db.labels() YIELD label",
                mapping=pg_mapping,
            )
        assert exc_info.value.code == "UNSUPPORTED"

    def test_call_without_registry_rejected(self, pg_mapping):
        with pytest.raises(CoreError) as exc_info:
            translate(
                "CALL arango.fulltext('users', 'name', 'test') YIELD doc",
                mapping=pg_mapping,
            )
        assert exc_info.value.code == "EXTENSIONS_DISABLED"


class TestInQueryCall:
    def test_call_with_return(self, all_registry, pg_mapping):
        out = translate(
            "CALL arango.fulltext('posts', 'body', 'graph') YIELD doc RETURN doc.title AS title",
            mapping=pg_mapping, registry=all_registry,
        )
        assert "FULLTEXT('posts', 'body', 'graph')" in out.aql
        assert "RETURN {title: doc.title}" in out.aql

    def test_match_then_call(self, all_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) CALL arango.near('locations', 40.7, -74.0, 10) YIELD loc RETURN n.name AS name, loc",
            mapping=pg_mapping, registry=all_registry,
        )
        assert "FOR n IN @@collection" in out.aql
        assert "FOR loc IN NEAR(" in out.aql
        assert "RETURN {name: n.name, loc: loc}" in out.aql
        assert out.bind_vars["@collection"] == "users"


class TestProcedureCompilers:
    def test_fulltext_wrong_args(self, all_registry, pg_mapping):
        with pytest.raises(CoreError) as exc_info:
            translate(
                "CALL arango.fulltext('users', 'name') YIELD doc",
                mapping=pg_mapping, registry=all_registry,
            )
        assert exc_info.value.code == "UNSUPPORTED"

    def test_near_wrong_args(self, all_registry, pg_mapping):
        with pytest.raises(CoreError) as exc_info:
            translate(
                "CALL arango.near('places') YIELD loc",
                mapping=pg_mapping, registry=all_registry,
            )
        assert exc_info.value.code == "UNSUPPORTED"

    def test_within_wrong_args(self, all_registry, pg_mapping):
        with pytest.raises(CoreError) as exc_info:
            translate(
                "CALL arango.within('places', 40.7) YIELD loc",
                mapping=pg_mapping, registry=all_registry,
            )
        assert exc_info.value.code == "UNSUPPORTED"

    def test_unknown_extension_procedure(self, all_registry, pg_mapping):
        with pytest.raises(CoreError) as exc_info:
            translate(
                "CALL arango.nonexistent() YIELD x",
                mapping=pg_mapping, registry=all_registry,
            )
        assert exc_info.value.code == "UNKNOWN_EXTENSION"
