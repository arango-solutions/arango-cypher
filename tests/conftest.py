from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.corpus import CorpusCase, load_all_cases


@pytest.fixture(scope="session")
def corpus_cases() -> list[CorpusCase]:
    cases_dir = Path(__file__).parent / "fixtures" / "cases"
    return load_all_cases(cases_dir)


@pytest.fixture(autouse=True)
def _reset_tenant_plan_cache():
    """Isolate the module-global Layer-5 plan-shape cache (MT-6) per test.

    The certification LRU lives at module scope in
    ``arango_cypher.tenant_plan_validator``; without a reset a shape
    certified by one test could turn a later test's real-EXPLAIN into a
    silent cache hit (skipping the round-trip the later test may assert
    on). Clearing before each test keeps validator behaviour
    deterministic regardless of order.
    """
    from arango_cypher.tenant_plan_validator import reset_plan_cache

    reset_plan_cache()
    yield
