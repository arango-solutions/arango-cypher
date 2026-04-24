"""Golden tests: verify all Northwind corpus queries translate without error."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

_CORPUS_FILE = Path(__file__).resolve().parent / "fixtures" / "datasets" / "northwind" / "query-corpus.yml"
_CORPUS = yaml.safe_load(_CORPUS_FILE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("query", _CORPUS, ids=lambda q: q["id"])
def test_northwind_translate(query: dict) -> None:
    mapping = mapping_bundle_for("northwind_pg")
    out = translate(query["cypher"], mapping=mapping)

    assert out.aql.strip(), f"[{query['id']}] empty AQL output"
    assert "FOR" in out.aql or "LET" in out.aql, (
        f"[{query['id']}] expected AQL to have FOR/LET, got:\n{out.aql}"
    )
