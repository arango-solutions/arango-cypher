"""``reduce(acc = init, x IN list | body)`` — sum-fold support.

AQL has no general fold, so only the numeric sum-fold shape (`acc + f(x)`,
accumulator once, all top-level `+`) is lowered — to
`(init + SUM((FOR x IN list RETURN f(x))))`. Every other accumulation (`*`,
string concatenation, multi-reference, etc.) raises a clear capability error
rather than mis-translating.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from arango_query_core import CoreError
from tests.helpers.mapping_fixtures import mapping_bundle_for


@pytest.fixture(scope="module")
def pg():
    return mapping_bundle_for("movies_pg")


class TestReduceSumFold:
    def test_simple_sum(self, pg):
        out = translate("RETURN reduce(s = 0, x IN [1, 2, 3] | s + x) AS total", mapping=pg)
        assert "(0 + SUM((FOR x IN [1,2,3] RETURN x)))" in out.aql

    def test_nonzero_init(self, pg):
        out = translate("RETURN reduce(s = 10, x IN [1, 2, 3] | s + x) AS total", mapping=pg)
        assert "(10 + SUM((FOR x IN [1,2,3] RETURN x)))" in out.aql

    def test_mapped_element(self, pg):
        out = translate("RETURN reduce(s = 0, x IN [1, 2, 3] | s + x * 2) AS total", mapping=pg)
        assert "SUM((FOR x IN [1,2,3] RETURN (x * 2)))" in out.aql

    def test_accumulator_second(self, pg):
        out = translate("RETURN reduce(s = 0, x IN [1, 2, 3] | x + s) AS total", mapping=pg)
        assert "(0 + SUM((FOR x IN [1,2,3] RETURN x)))" in out.aql

    def test_over_property_list(self, pg):
        out = translate(
            "MATCH (p:Person) RETURN reduce(t = 0, r IN p.roles | t + 1) AS n",
            mapping=pg,
        )
        assert "(0 + SUM((FOR r IN p.roles RETURN 1)))" in out.aql


class TestReduceUnsupported:
    @pytest.mark.parametrize(
        "query",
        [
            "RETURN reduce(s = 1, x IN [1, 2, 3] | s * x) AS p",  # product
            'RETURN reduce(s = "", x IN ["a", "b"] | s + x + "!") AS c',  # concat
            "RETURN reduce(s = 0, x IN [1, 2] | s + s) AS c",  # acc referenced twice
            "RETURN reduce(s = 0, x IN [1, 2] | s - x) AS c",  # subtraction fold
        ],
    )
    def test_non_sum_fold_rejected(self, pg, query):
        with pytest.raises(CoreError) as exc:
            translate(query, mapping=pg)
        assert exc.value.code == "NOT_IMPLEMENTED"
