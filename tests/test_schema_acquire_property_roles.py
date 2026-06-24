"""Tests for domain-agnostic property role classification.

The classifier lets downstream code (entity resolver, NL prompt) reason about a
property by *what kind of value it holds* — identifier vs name vs category vs
date vs number — instead of hardcoding field-name lists. These tests pin the
heuristic so a refactor can't silently regress it.
"""

from __future__ import annotations

import pytest

from arango_cypher.schema_acquire import (
    ROLE_BOOLEAN,
    ROLE_CATEGORICAL,
    ROLE_FREE_TEXT,
    ROLE_IDENTIFIER,
    ROLE_NAME,
    ROLE_NUMERIC,
    ROLE_TEMPORAL,
    _classify_property_role,
    _profile_property_values,
)


def _role(values, dominant_type="string", numeric_like=False):
    return _classify_property_role(values, dominant_type, numeric_like)


class TestClassifyPropertyRole:
    def test_identifier_unique_short_tokens(self) -> None:
        vals = ["AAPL", "MSFT", "GOOGL", "TSLA", "JPM", "CINF", "BRK"]
        assert _role(vals) == ROLE_IDENTIFIER

    def test_name_human_readable_multiword(self) -> None:
        vals = [
            "Apple Inc",
            "Microsoft Corporation",
            "Alphabet Inc",
            "JPMorgan Chase",
            "Tesla Motors",
        ]
        assert _role(vals) == ROLE_NAME

    def test_categorical_low_cardinality(self) -> None:
        # Realistic sample size: few distinct values across many rows is the
        # signal (distinct-ratio is meaningless on a handful of samples).
        vals = (["ACTIVE"] * 8) + (["INACTIVE"] * 7) + (["PENDING"] * 5)
        assert _role(vals) == ROLE_CATEGORICAL

    def test_temporal_iso_dates(self) -> None:
        vals = ["2020-01-01", "2021-05-03", "2022-12-31", "2019-07-15"]
        assert _role(vals) == ROLE_TEMPORAL

    def test_temporal_iso_datetimes(self) -> None:
        vals = ["2020-01-01T09:30:00Z", "2021-05-03T14:00:00", "2022-12-31T23:59:59Z"]
        assert _role(vals) == ROLE_TEMPORAL

    def test_numeric_native(self) -> None:
        assert _role([1, 2, 3, 4], dominant_type="number") == ROLE_NUMERIC

    def test_numeric_like_strings(self) -> None:
        assert _role(["1.5", "2.0", "3.14"], numeric_like=True) == ROLE_NUMERIC

    def test_boolean(self) -> None:
        assert _role([True, False, True], dominant_type="boolean") == ROLE_BOOLEAN

    def test_free_text_long_values(self) -> None:
        vals = [
            "The company reported strong quarterly earnings driven by cloud growth.",
            "Management guided to higher margins amid cost discipline and demand.",
            "Risk factors include regulatory scrutiny and macroeconomic headwinds.",
        ]
        assert _role(vals) == ROLE_FREE_TEXT

    def test_empty_is_other(self) -> None:
        assert _role([]) != ROLE_IDENTIFIER  # no signal → not an identifier


class TestProfileEmitsRole:
    def test_profile_includes_role(self) -> None:
        profile = _profile_property_values(["AAPL", "MSFT", "GOOGL", "TSLA"], 4)
        assert profile.get("role") == ROLE_IDENTIFIER

    def test_profile_name_role(self) -> None:
        profile = _profile_property_values(
            ["Apple Inc", "Microsoft Corporation", "Alphabet Inc"], 3
        )
        assert profile.get("role") == ROLE_NAME
