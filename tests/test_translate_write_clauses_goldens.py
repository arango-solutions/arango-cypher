from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

WRITE_CLAUSE_IDS = (
    # SET
    [f"C{n}" for n in range(400, 406)]
    # DELETE
    + [f"C{n}" for n in range(410, 414)]
    # MERGE (node)
    + [f"C{n}" for n in range(420, 425)]
    # MERGE (relationship)
    + [f"C{n}" for n in range(425, 428)]
    # CREATE extras
    + [f"C{n}" for n in range(430, 432)]
)


@pytest.mark.parametrize("case_id", WRITE_CLAUSE_IDS)
def test_translate_write_clauses_goldens(corpus_cases, case_id: str):
    case = next(c for c in corpus_cases if c.id == case_id)
    mapping = mapping_bundle_for(case.mapping_fixture)

    out = translate(case.cypher, mapping=mapping, params=case.params)

    assert case.expected_aql is not None, f"Golden AQL must be filled for {case_id}"
    assert out.aql.strip() == case.expected_aql.strip(), (
        f"AQL mismatch for {case_id}:\n  got:      {out.aql.strip()!r}\n  expected: {case.expected_aql.strip()!r}"
    )
    assert out.bind_vars == case.expected_bind_vars, (
        f"Bind vars mismatch for {case_id}:\n  got:      {out.bind_vars}\n  expected: {case.expected_bind_vars}"
    )
