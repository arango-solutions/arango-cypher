from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.tck.gherkin import parse_feature
from tests.tck.runner import run_feature


def test_tck_feature_parser_smoke():
    # Smoke test the parser on a tiny in-repo feature file.
    p = Path(__file__).resolve().parent / "features" / "sample.feature"
    feat = parse_feature(p)
    assert feat.name
    assert feat.scenarios
    assert feat.scenarios[0].steps


@pytest.mark.integration
def test_tck_runner_smoke_integration():
    if os.environ.get("RUN_TCK") != "1":
        pytest.skip("Set RUN_TCK=1 to run TCK harness integration smoke")
    p = Path(__file__).resolve().parent / "features" / "sample.feature"
    rep = run_feature(p, db_name="tck_smoke_db", mapping_fixture="cypher_lpg_fixture")
    assert rep["feature"]
    assert rep["scenarios"] >= 1
    assert set(rep["counts"].keys()) == {"passed", "skipped", "failed"}

