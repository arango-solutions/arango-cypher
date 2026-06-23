"""WP-C2: list subscript and slice operators.

Cypher ``list[i]`` (subscript, negative indices from the end) and the half-open
slices ``list[i..j]`` / ``list[i..]`` / ``list[..j]`` / ``list[..]``. These map
to AQL native array access (``(expr)[i]``, which supports negative indices) and
``SLICE(array, start[, length])`` respectively.
"""

from __future__ import annotations

import pytest

from arango_cypher import translate
from tests.helpers.mapping_fixtures import mapping_bundle_for

_LIST = "[x IN ['a','b','c','d'] | x]"


@pytest.fixture(scope="module")
def bundle():
    return mapping_bundle_for("finreflectkg")


def _aql(bundle, expr: str) -> str:
    out = translate(f"MATCH (o:ORG) RETURN {expr} AS r", mapping=bundle)
    return out.aql


def test_subscript_positive_index(bundle) -> None:
    assert "[0]" in _aql(bundle, f"{_LIST}[0]")


def test_subscript_negative_index(bundle) -> None:
    # AQL array access handles negative indices natively.
    assert "[(-1)]" in _aql(bundle, f"{_LIST}[-1]")


def test_labels_subscript(bundle) -> None:
    # The canonical "group/label by first label" idiom.
    out = translate("MATCH (n:ORG) RETURN labels(n)[0] AS t", mapping=bundle)
    assert ")[0]" in out.aql


def test_slice_both_bounds_is_half_open_length(bundle) -> None:
    aql = _aql(bundle, f"{_LIST}[1..3]")
    assert "SLICE(" in aql
    assert "(3) - (1)" in aql  # length = end - start


def test_slice_open_end(bundle) -> None:
    aql = _aql(bundle, f"{_LIST}[2..]")
    assert "SLICE(" in aql
    assert ", 2)" in aql  # start only, no length


def test_slice_open_start(bundle) -> None:
    aql = _aql(bundle, f"{_LIST}[..2]")
    assert "SLICE(" in aql
    assert ", 0, 2)" in aql  # start defaults to 0


def test_slice_full(bundle) -> None:
    # "[..]" is a no-op slice over the whole list.
    aql = _aql(bundle, f"{_LIST}[..]")
    assert "SLICE(" not in aql


def test_in_operator_still_works(bundle) -> None:
    # Regression: the IN list operator must be unaffected by subscript/slice.
    out = translate(
        "MATCH (o:ORG) WHERE o.name IN ['aapl','msft'] RETURN o.name",
        mapping=bundle,
    )
    assert " IN " in out.aql
