"""Tests for arango.* vector search and geo extension compilers."""

from __future__ import annotations

import pytest

from arango_cypher import register_all_extensions, translate
from arango_query_core import CoreError, ExtensionPolicy, ExtensionRegistry
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture()
def registry() -> ExtensionRegistry:
    r = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
    register_all_extensions(r)
    return r


@pytest.fixture()
def pg():
    return mapping_bundle_for("pg")


class TestVector:
    def test_cosine_similarity(self, registry, pg):
        out = translate(
            "MATCH (n:User) RETURN arango.cosine_similarity(n.embedding, $q) AS sim",
            mapping=pg,
            registry=registry,
            params={"q": [0.1, 0.2]},
        )
        assert "COSINE_SIMILARITY(n.embedding, @q)" in out.aql

    def test_l2_distance(self, registry, pg):
        out = translate(
            "MATCH (n:User) RETURN arango.l2_distance(n.embedding, $q) AS dist",
            mapping=pg,
            registry=registry,
            params={"q": [0.1, 0.2]},
        )
        assert "L2_DISTANCE(n.embedding, @q)" in out.aql

    def test_approx_near_cosine(self, registry, pg):
        out = translate(
            "MATCH (n:User) RETURN arango.approx_near_cosine(n.embedding, $q) AS sim",
            mapping=pg,
            registry=registry,
            params={"q": [0.1, 0.2]},
        )
        assert "APPROX_NEAR_COSINE(n.embedding, @q)" in out.aql

    def test_approx_near_l2(self, registry, pg):
        out = translate(
            "MATCH (n:User) RETURN arango.approx_near_l2(n.embedding, $q) AS dist",
            mapping=pg,
            registry=registry,
            params={"q": [0.1, 0.2]},
        )
        assert "APPROX_NEAR_L2(n.embedding, @q)" in out.aql

    def test_cosine_bad_args(self, registry, pg):
        with pytest.raises(CoreError, match="2 arguments"):
            translate(
                "MATCH (n:User) RETURN arango.cosine_similarity(n.embedding) AS sim",
                mapping=pg,
                registry=registry,
            )


class TestGeo:
    def test_distance(self, registry, pg):
        out = translate(
            "MATCH (n:User) RETURN arango.distance(n.lat, n.lon, 40.7, -74.0) AS d",
            mapping=pg,
            registry=registry,
        )
        assert "DISTANCE(n.lat, n.lon, 40.7," in out.aql

    def test_geo_distance(self, registry, pg):
        out = translate(
            "MATCH (n:User) RETURN arango.geo_distance(n.loc, $target) AS d",
            mapping=pg,
            registry=registry,
            params={"target": {"type": "Point", "coordinates": [0, 0]}},
        )
        assert "GEO_DISTANCE(n.loc, @target)" in out.aql

    def test_geo_contains_in_where(self, registry, pg):
        out = translate(
            "MATCH (n:User) WHERE arango.geo_contains($poly, arango.geo_point(n.lon, n.lat)) RETURN n",
            mapping=pg,
            registry=registry,
            params={"poly": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}},
        )
        assert "GEO_CONTAINS(@poly, GEO_POINT(n.lon, n.lat))" in out.aql

    def test_geo_intersects(self, registry, pg):
        out = translate(
            "MATCH (n:User) WHERE arango.geo_intersects(n.area, $region) RETURN n",
            mapping=pg,
            registry=registry,
            params={"region": {}},
        )
        assert "GEO_INTERSECTS(n.area, @region)" in out.aql

    def test_geo_point(self, registry, pg):
        out = translate(
            "MATCH (n:User) RETURN arango.geo_point(n.lon, n.lat) AS pt",
            mapping=pg,
            registry=registry,
        )
        assert "GEO_POINT(n.lon, n.lat)" in out.aql

    def test_geo_in_range(self, registry, pg):
        out = translate(
            "MATCH (n:User) WHERE arango.geo_in_range(n.loc, $center, 0, 1000) RETURN n",
            mapping=pg,
            registry=registry,
            params={"center": {}},
        )
        assert "GEO_IN_RANGE(n.loc, @center, 0, 1000)" in out.aql

    def test_distance_bad_args(self, registry, pg):
        with pytest.raises(CoreError, match="4 arguments"):
            translate(
                "MATCH (n:User) RETURN arango.distance(n.lat, n.lon) AS d",
                mapping=pg,
                registry=registry,
            )
