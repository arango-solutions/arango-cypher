from __future__ import annotations

import pytest

from arango_cypher import parse_cypher
from arango_query_core import CoreError


def test_parse_all_corpus_cases(corpus_cases):
    # Parser should accept our v0.1 corpus, even before translation is implemented.
    for case in corpus_cases:
        try:
            parse_cypher(case.cypher)
        except CoreError as e:
            raise AssertionError(f"Failed to parse {case.id}: {e}") from e


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "MATCH (n RETURN n",  # missing ')'
    ],
)
def test_parse_rejects_invalid_input(bad: str):
    with pytest.raises(CoreError):
        parse_cypher(bad)
