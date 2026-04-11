"""ArangoDB geo-spatial extension compilers (arango.distance, arango.geo_distance, etc.)."""

from __future__ import annotations

from typing import Any

from arango_query_core import CoreError, ExtensionRegistry


def _compile_distance(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.distance(lat1, lon1, lat2, lon2)`` → ``DISTANCE(lat1, lon1, lat2, lon2)``."""
    if len(args) != 4:
        raise CoreError(
            "arango.distance expects 4 arguments: (lat1, lon1, lat2, lon2)",
            code="UNSUPPORTED",
        )
    return f"DISTANCE({', '.join(args)})"


def _compile_geo_distance(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.geo_distance(a, b[, ellipsoid])`` → ``GEO_DISTANCE(a, b[, ellipsoid])``."""
    if len(args) < 2 or len(args) > 3:
        raise CoreError(
            "arango.geo_distance expects 2-3 arguments: (geoJsonA, geoJsonB[, ellipsoid])",
            code="UNSUPPORTED",
        )
    return f"GEO_DISTANCE({', '.join(args)})"


def _compile_geo_contains(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.geo_contains(a, b)`` → ``GEO_CONTAINS(a, b)``."""
    if len(args) != 2:
        raise CoreError(
            "arango.geo_contains expects 2 arguments: (geoJsonA, geoJsonB)",
            code="UNSUPPORTED",
        )
    return f"GEO_CONTAINS({', '.join(args)})"


def _compile_geo_intersects(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.geo_intersects(a, b)`` → ``GEO_INTERSECTS(a, b)``."""
    if len(args) != 2:
        raise CoreError(
            "arango.geo_intersects expects 2 arguments: (geoJsonA, geoJsonB)",
            code="UNSUPPORTED",
        )
    return f"GEO_INTERSECTS({', '.join(args)})"


def _compile_geo_in_range(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.geo_in_range(a, b, low, high[, incLow, incHigh])`` → ``GEO_IN_RANGE(...)``."""
    if len(args) < 4 or len(args) > 6:
        raise CoreError(
            "arango.geo_in_range expects 4-6 arguments: (a, b, low, high[, incLow, incHigh])",
            code="UNSUPPORTED",
        )
    return f"GEO_IN_RANGE({', '.join(args)})"


def _compile_geo_point(args: list[str], bind_vars: dict[str, Any]) -> str:
    """``arango.geo_point(lon, lat)`` → ``GEO_POINT(lon, lat)``."""
    if len(args) != 2:
        raise CoreError(
            "arango.geo_point expects 2 arguments: (longitude, latitude)",
            code="UNSUPPORTED",
        )
    return f"GEO_POINT({', '.join(args)})"


def register_geo_extensions(registry: ExtensionRegistry) -> None:
    """Register ArangoDB geo-spatial extension function compilers."""
    registry.register_function("arango.distance", _compile_distance)
    registry.register_function("arango.geo_distance", _compile_geo_distance)
    registry.register_function("arango.geo_contains", _compile_geo_contains)
    registry.register_function("arango.geo_intersects", _compile_geo_intersects)
    registry.register_function("arango.geo_in_range", _compile_geo_in_range)
    registry.register_function("arango.geo_point", _compile_geo_point)
