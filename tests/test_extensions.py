"""Tests for arango.* extension function registry and built-in search compilers."""

from __future__ import annotations

import pytest

from arango_cypher import register_search_extensions, translate
from arango_query_core import CoreError, ExtensionPolicy, ExtensionRegistry
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture()
def search_registry() -> ExtensionRegistry:
    r = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    register_search_extensions(r)
    return r


@pytest.fixture()
def pg_mapping():
    return mapping_bundle_for("pg")


class TestBM25:
    def test_bm25_in_return(self, search_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) RETURN arango.bm25(n) AS score",
            mapping=pg_mapping, registry=search_registry,
        )
        assert "BM25(n)" in out.aql

    def test_bm25_with_params(self, search_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) RETURN arango.bm25(n, 1.2, 0.75) AS score",
            mapping=pg_mapping, registry=search_registry,
        )
        assert "BM25(n, 1.2, 0.75)" in out.aql

    def test_bm25_in_where(self, search_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) WHERE arango.bm25(n) > 0.5 RETURN n",
            mapping=pg_mapping, registry=search_registry,
        )
        assert "BM25(n) > 0.5" in out.aql

    def test_bm25_no_args_rejected(self, search_registry, pg_mapping):
        with pytest.raises(CoreError) as exc_info:
            translate(
                "MATCH (n:User) RETURN arango.bm25() AS score",
                mapping=pg_mapping, registry=search_registry,
            )
        assert exc_info.value.code == "UNSUPPORTED"


class TestTFIDF:
    def test_tfidf_basic(self, search_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) RETURN arango.tfidf(n) AS score",
            mapping=pg_mapping, registry=search_registry,
        )
        assert "TFIDF(n)" in out.aql

    def test_tfidf_with_normalize(self, search_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) RETURN arango.tfidf(n, true) AS score",
            mapping=pg_mapping, registry=search_registry,
        )
        assert "TFIDF(n, true)" in out.aql


class TestAnalyzer:
    def test_analyzer_in_where(self, search_registry, pg_mapping):
        out = translate(
            "MATCH (n:User) WHERE arango.analyzer(n.name, 'text_en') = 'alice' RETURN n",
            mapping=pg_mapping, registry=search_registry,
        )
        assert "ANALYZER(n.name, 'text_en')" in out.aql

    def test_analyzer_wrong_args(self, search_registry, pg_mapping):
        with pytest.raises(CoreError) as exc_info:
            translate(
                "MATCH (n:User) RETURN arango.analyzer(n.name) AS a",
                mapping=pg_mapping, registry=search_registry,
            )
        assert exc_info.value.code == "UNSUPPORTED"


class TestRegistryErrors:
    def test_no_registry_raises(self, pg_mapping):
        with pytest.raises(CoreError) as exc_info:
            translate(
                "MATCH (n:User) RETURN arango.bm25(n) AS score",
                mapping=pg_mapping,
            )
        assert exc_info.value.code == "EXTENSIONS_DISABLED"

    def test_unknown_extension_raises(self, search_registry, pg_mapping):
        with pytest.raises(CoreError) as exc_info:
            translate(
                "MATCH (n:User) RETURN arango.nonexistent(n) AS score",
                mapping=pg_mapping, registry=search_registry,
            )
        assert exc_info.value.code == "UNKNOWN_EXTENSION"

    def test_allowlist_blocks_unlisted(self, pg_mapping):
        r = ExtensionRegistry(
            policy=ExtensionPolicy(enabled=True, allowlist={"arango.bm25"}),
        )
        register_search_extensions(r)
        with pytest.raises(CoreError) as exc_info:
            translate(
                "MATCH (n:User) RETURN arango.tfidf(n) AS score",
                mapping=pg_mapping, registry=r,
            )
        assert exc_info.value.code == "EXTENSION_NOT_ALLOWED"
