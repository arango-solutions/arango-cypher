"""Execution-grounded guards for the FinReflectKG corpus (WP-V1).

Transpile-success is not semantic correctness. These tests run the translated
AQL against a *live* FinReflectKG database and assert the **shape** of what
comes back — the concern the "show me, as a graph" defect surfaced:

* a scalar/projection ``RETURN n.name`` yields rows of scalar values, while
* a path ``RETURN p`` yields graph objects (``{nodes: [...], edges: [...]}``)
  the UI can actually render.

They are opt-in (see :mod:`tests.helpers.live_db`): marked ``live`` and named
with ``live`` so the default ``-k "not live"`` suite skips them, and they skip
(never fail) when ``ARANGO_URL`` is unset. The live DB is large, so every query
is tightly ``LIMIT``-bounded and runtime-capped.
"""

from __future__ import annotations

import pytest

from tests.helpers.live_db import execute_translated, require_live_db
from tests.helpers.mapping_fixtures import mapping_bundle_for

pytestmark = pytest.mark.live

LIVE_DATABASE = "FinReflectKG"


@pytest.fixture(scope="module")
def live_db():
    return require_live_db(LIVE_DATABASE)


@pytest.fixture(scope="module")
def bundle():
    return mapping_bundle_for("finreflectkg")


def _is_path_object(row: object) -> bool:
    """A rendered path is a mapping with list-valued ``nodes`` and ``edges``."""
    return (
        isinstance(row, dict)
        and isinstance(row.get("nodes"), list)
        and isinstance(row.get("edges"), list)
    )


class TestResultShape:
    """The path-vs-scalar distinction the 'as a graph' fix is about."""

    def test_scalar_projection_returns_scalar_values(self, live_db, bundle):
        rows, aql = execute_translated(
            live_db, "MATCH (n:ORG) RETURN n.name AS name LIMIT 3", bundle
        )
        assert rows, f"expected ORG rows; AQL was:\n{aql}"
        for row in rows:
            assert isinstance(row, dict), f"row not a projection: {row!r}"
            assert set(row.keys()) == {"name"}, f"unexpected projection keys: {row!r}"
            assert row["name"] is None or isinstance(row["name"], str)
            assert not _is_path_object(row), "scalar projection leaked a path object"

    def test_path_return_returns_graph_objects(self, live_db, bundle):
        rows, aql = execute_translated(
            live_db,
            "MATCH p = (a:ORG)-[:Depends_On]->(b:ORG) RETURN p LIMIT 3",
            bundle,
        )
        assert rows, f"expected Depends_On paths; AQL was:\n{aql}"
        for row in rows:
            assert _is_path_object(row), f"path RETURN did not yield a graph: {row!r}"
            # A 1-hop path: two endpoint nodes and one edge, each a real document.
            assert len(row["nodes"]) >= 2
            assert len(row["edges"]) >= 1
            for node in row["nodes"]:
                assert isinstance(node, dict) and node.get("_id")
            for edge in row["edges"]:
                assert isinstance(edge, dict) and edge.get("_from") and edge.get("_to")

    def test_graph_intent_shape_differs_from_scalar(self, live_db, bundle):
        """The same relationship, asked two ways, returns structurally different
        results — the heart of the 'return a graph' intent fix."""
        scalar_rows, _ = execute_translated(
            live_db,
            "MATCH (a:ORG)-[:Depends_On]->(b:ORG) RETURN b.name AS name LIMIT 3",
            bundle,
        )
        path_rows, _ = execute_translated(
            live_db,
            "MATCH p = (a:ORG)-[:Depends_On]->(b:ORG) RETURN p LIMIT 3",
            bundle,
        )
        assert scalar_rows and path_rows
        assert not any(_is_path_object(r) for r in scalar_rows)
        assert all(_is_path_object(r) for r in path_rows)


class TestTraversalSemantics:
    """A couple of corpus-representative traversals return well-formed rows."""

    def test_typed_two_hop_projection(self, live_db, bundle):
        rows, aql = execute_translated(
            live_db,
            "MATCH (org:ORG)-[:Discloses]->(m:FIN_METRIC) "
            "RETURN org.name AS organization, m.name AS metric LIMIT 5",
            bundle,
        )
        assert rows, f"expected Discloses rows; AQL was:\n{aql}"
        for row in rows:
            assert set(row.keys()) == {"organization", "metric"}

    def test_variable_length_path_returns_chain(self, live_db, bundle):
        rows, aql = execute_translated(
            live_db,
            "MATCH p = (a:ORG)-[:Depends_On*2..3]->(b:ORG) "
            "RETURN [n IN nodes(p) | n.name] AS chain LIMIT 3",
            bundle,
        )
        assert rows, f"expected variable-length chains; AQL was:\n{aql}"
        for row in rows:
            assert isinstance(row.get("chain"), list)
            # 2..3 hops => 3 or 4 nodes in the chain.
            assert 3 <= len(row["chain"]) <= 4
