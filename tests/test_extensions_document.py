"""Tests for document-centric arango.* extension functions."""

from __future__ import annotations

import pytest

from arango_cypher import register_all_extensions, translate
from arango_cypher.extensions.document import register_document_extensions
from arango_query_core import CoreError, ExtensionPolicy, ExtensionRegistry
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture()
def doc_registry() -> ExtensionRegistry:
    r = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    register_document_extensions(r)
    return r


@pytest.fixture()
def all_registry() -> ExtensionRegistry:
    r = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    register_all_extensions(r)
    return r


@pytest.fixture()
def pg_mapping():
    return mapping_bundle_for("pg")


class TestAttributes:
    def test_basic(self, doc_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) RETURN arango.attributes(n) AS attrs",
            mapping=pg_mapping,
            registry=doc_registry,
        )
        assert "ATTRIBUTES(n)" in out.aql

    def test_with_remove_internal(self, doc_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) RETURN arango.attributes(n, true) AS attrs",
            mapping=pg_mapping,
            registry=doc_registry,
        )
        assert "ATTRIBUTES(n, true)" in out.aql

    def test_no_args_rejected(self, doc_registry, pg_mapping):
        with pytest.raises(CoreError) as exc_info:
            translate(
                "MATCH (n:User) RETURN arango.attributes() AS x",
                mapping=pg_mapping,
                registry=doc_registry,
            )
        assert exc_info.value.code == "UNSUPPORTED"


class TestHas:
    def test_in_where(self, doc_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) WHERE arango.has(n, 'email') RETURN n",
            mapping=pg_mapping,
            registry=doc_registry,
        )
        assert "HAS(n, 'email')" in out.aql

    def test_wrong_args(self, doc_registry, pg_mapping):
        with pytest.raises(CoreError):
            translate(
                "MATCH (n:User) RETURN arango.has(n) AS x",
                mapping=pg_mapping,
                registry=doc_registry,
            )


class TestKeep:
    def test_basic(self, doc_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) RETURN arango.keep(n, 'name', 'email') AS trimmed",
            mapping=pg_mapping,
            registry=doc_registry,
        )
        assert "KEEP(n, 'name', 'email')" in out.aql


class TestUnset:
    def test_basic(self, doc_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) RETURN arango.unset(n, '_key', '_rev') AS cleaned",
            mapping=pg_mapping,
            registry=doc_registry,
        )
        assert "UNSET(n, '_key', '_rev')" in out.aql


class TestFlatten:
    def test_basic(self, doc_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) RETURN arango.flatten(n.tags) AS flat",
            mapping=pg_mapping,
            registry=doc_registry,
        )
        assert "FLATTEN(n.tags)" in out.aql

    def test_with_depth(self, doc_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) RETURN arango.flatten(n.nested, 2) AS flat",
            mapping=pg_mapping,
            registry=doc_registry,
        )
        assert "FLATTEN(n.nested, 2)" in out.aql


class TestDocument:
    def test_by_id(self, doc_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) RETURN arango.document(n.companyId) AS company",
            mapping=pg_mapping,
            registry=doc_registry,
        )
        assert "DOCUMENT(n.companyId)" in out.aql

    def test_wrong_args(self, doc_registry, pg_mapping):
        with pytest.raises(CoreError):
            translate(
                "MATCH (n:User) RETURN arango.document() AS x",
                mapping=pg_mapping,
                registry=doc_registry,
            )


class TestValue:
    def test_dynamic_path(self, doc_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) RETURN arango.value(n, 'address.zip') AS zip",
            mapping=pg_mapping,
            registry=doc_registry,
        )
        assert "VALUE(n, 'address.zip')" in out.aql


class TestValues:
    def test_basic(self, doc_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) RETURN arango.values(n) AS vals",
            mapping=pg_mapping,
            registry=doc_registry,
        )
        assert "VALUES(n)" in out.aql


class TestZip:
    def test_basic(self, doc_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) RETURN arango.zip(n.keys, n.vals) AS zipped",
            mapping=pg_mapping,
            registry=doc_registry,
        )
        assert "ZIP(n.keys, n.vals)" in out.aql


class TestParseIdentifier:
    def test_basic(self, doc_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) RETURN arango.parse_identifier(n._id) AS parsed",
            mapping=pg_mapping,
            registry=doc_registry,
        )
        assert "PARSE_IDENTIFIER(n._id)" in out.aql
