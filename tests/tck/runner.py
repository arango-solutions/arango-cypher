from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

from arango import ArangoClient

from arango_cypher import translate
from arango_query_core import CoreError
from arango_query_core.exec import AqlExecutor
from tests.helpers.mapping_fixtures import mapping_bundle_for
from tests.integration.seed import _ensure_doc_collection, _ensure_edge_collection, _reset_collection

from .gherkin import Feature, Scenario, Step, parse_feature


@dataclass(frozen=True)
class ScenarioOutcome:
    status: str  # passed|skipped|failed
    reason: str | None = None


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if isinstance(v, str) and v else default


def _connect_db(db_name: str):
    url = _env("ARANGO_URL", "http://localhost:8529")
    user = _env("ARANGO_USER", "root")
    pw = _env("ARANGO_PASS", "openSesame")
    client = ArangoClient(hosts=url)
    sys_db = client.db("_system", username=user, password=pw)
    if not sys_db.has_database(db_name):
        sys_db.create_database(db_name)
    return client.db(db_name, username=user, password=pw)


def _reset_tck_graph(db: Any) -> None:
    # Minimal empty graph: create/truncate generic collections.
    nodes = _ensure_doc_collection(db, "nodes")
    edges = _ensure_edge_collection(db, "edges")
    _reset_collection(nodes)
    _reset_collection(edges)


def _parse_table(table: list[list[str]] | None) -> list[dict[str, Any]]:
    if not table:
        return []
    if len(table) < 2:
        return []
    headers = table[0]
    rows = table[1:]
    out: list[dict[str, Any]] = []
    for r in rows:
        obj: dict[str, Any] = {}
        for k, v in zip(headers, r, strict=False):
            obj[k] = v
        out.append(obj)
    return out


def run_scenario(
    scenario: Scenario,
    *,
    db_name: str,
    mapping_fixture: str,
) -> ScenarioOutcome:
    """
    Run a single scenario for the subset of steps we support.

    Current intent (Phase A): provide infrastructure and clean skipping.
    """
    db = _connect_db(db_name)
    params: dict[str, Any] = {}
    cypher: str | None = None
    expected_rows: list[dict[str, Any]] | None = None
    expect_empty: bool = False

    # We only support scenarios that:
    # - start from an empty graph
    # - do not contain CREATE/SET/DELETE/etc in setup
    # - execute exactly one query and assert "empty" or a simple table
    for step in scenario.steps:
        s = step.text

        if step.keyword == "Given" and s in {"an empty graph", "the empty graph"}:
            _reset_tck_graph(db)
            continue

        if step.keyword == "Given" and s.startswith("having executed:"):
            # Setup queries are out of scope in v0 harness; skip.
            return ScenarioOutcome(status="skipped", reason="setup queries not supported yet")

        if step.keyword == "Given" and s.startswith("parameters are:"):
            rows = _parse_table(step.data_table)
            # TCK parameter formats vary; keep as raw strings for now.
            for row in rows:
                if row:
                    k = next(iter(row.keys()))
                    params[k] = next(iter(row.values()))
            continue

        if step.keyword == "When" and s.startswith("executing query:"):
            if not step.doc_string:
                return ScenarioOutcome(status="skipped", reason="missing query docstring")
            cypher = step.doc_string.strip()
            continue

        if step.keyword == "Then" and s == "the result should be empty":
            expect_empty = True
            continue

        if step.keyword == "Then" and s.startswith("the result should be:"):
            expected_rows = _parse_table(step.data_table)
            continue

        # Everything else is currently out of scope.
        return ScenarioOutcome(status="skipped", reason=f"unsupported step: {step.keyword} {step.text}")

    if not cypher:
        return ScenarioOutcome(status="skipped", reason="no query executed")

    try:
        out = translate(cypher, mapping=mapping_bundle_for(mapping_fixture), params=params or None)
    except CoreError as e:
        return ScenarioOutcome(status="skipped", reason=f"translate skipped: {e.code}")

    rows = list(AqlExecutor(db).execute(out.to_aql_query()))
    if expect_empty:
        if rows:
            return ScenarioOutcome(status="failed", reason=f"expected empty, got {len(rows)} rows")
        return ScenarioOutcome(status="passed")
    if expected_rows is not None:
        if rows != expected_rows:
            return ScenarioOutcome(status="failed", reason="result mismatch")
        return ScenarioOutcome(status="passed")
    return ScenarioOutcome(status="skipped", reason="no assertion")


def run_feature(
    feature_path: Path,
    *,
    db_name: str,
    mapping_fixture: str,
) -> dict[str, Any]:
    feat: Feature = parse_feature(feature_path)
    outcomes: list[ScenarioOutcome] = []
    for sc in feat.scenarios:
        outcomes.append(run_scenario(sc, db_name=db_name, mapping_fixture=mapping_fixture))

    counts: dict[str, int] = {"passed": 0, "skipped": 0, "failed": 0}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1

    return {"feature": feat.name, "scenarios": len(feat.scenarios), "counts": counts}

