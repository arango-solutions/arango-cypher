"""Tests for ``_validate_aql_syntax`` — the structural gate on LLM-generated
AQL in the NL→AQL path.

Regression focus: the validator used to treat every ``INTO <name>`` token as
a collection reference, so a perfectly valid aggregation like
``COLLECT t = doc.type INTO group`` was rejected with
"unknown collection(s): group" and retried to death. Only the
``INSERT … INTO <collection>`` form actually names a collection.
"""

from __future__ import annotations

from arango_cypher.nl2cypher._aql import _validate_aql_syntax

KNOWN = {"Node", "relations"}


class TestCollectIntoIsNotACollection:
    def test_collect_into_group_is_accepted(self):
        aql = (
            "FOR e IN relations\n"
            "  COLLECT relType = e.type INTO group\n"
            "  RETURN { relType, count: LENGTH(group) }"
        )
        ok, err = _validate_aql_syntax(aql, known_collections=KNOWN)
        assert ok, err

    def test_with_count_into_var_is_accepted(self):
        aql = (
            "FOR n IN Node\n"
            "  COLLECT t = n.type WITH COUNT INTO count\n"
            "  SORT count DESC LIMIT 20\n"
            "  RETURN { entityType: t, count }"
        )
        ok, err = _validate_aql_syntax(aql, known_collections=KNOWN)
        assert ok, err

    def test_collect_into_with_projection_is_accepted(self):
        aql = "FOR a IN Node\n  COLLECT k = a.type INTO items = a.text\n  RETURN { k, items }"
        ok, err = _validate_aql_syntax(aql, known_collections=KNOWN)
        assert ok, err


class TestInsertIntoStillValidatesCollection:
    def test_insert_into_unknown_collection_is_rejected(self):
        aql = "INSERT { x: 1 } INTO Ghost"
        ok, err = _validate_aql_syntax(aql, known_collections=KNOWN)
        assert not ok
        assert "Ghost" in err

    def test_insert_into_known_collection_is_accepted(self):
        aql = 'INSERT { type: "x" } INTO Node'
        ok, err = _validate_aql_syntax(aql, known_collections=KNOWN)
        assert ok, err


class TestForInStillValidatesCollection:
    def test_for_in_unknown_collection_is_rejected(self):
        aql = "FOR d IN NotARealCollection RETURN d"
        ok, err = _validate_aql_syntax(aql, known_collections=KNOWN)
        assert not ok
        assert "NotARealCollection" in err

    def test_for_in_known_collection_is_accepted(self):
        aql = "FOR d IN Node LIMIT 5 RETURN d"
        ok, err = _validate_aql_syntax(aql, known_collections=KNOWN)
        assert ok, err

    def test_traversal_builtins_not_flagged(self):
        aql = "FOR v IN 1..1 OUTBOUND 'Node/1' relations RETURN v"
        ok, err = _validate_aql_syntax(aql, known_collections=KNOWN)
        assert ok, err


class TestStructuralChecks:
    def test_unbalanced_parens_rejected(self):
        ok, err = _validate_aql_syntax("FOR d IN Node RETURN (d", known_collections=KNOWN)
        assert not ok
        assert "parentheses" in err

    def test_for_without_terminal_rejected(self):
        ok, err = _validate_aql_syntax("FOR d IN Node FILTER d.x == 1", known_collections=KNOWN)
        assert not ok

    def test_empty_rejected(self):
        ok, err = _validate_aql_syntax("   ", known_collections=KNOWN)
        assert not ok
