"""Tests for the NL-corrections store and its FewShotIndex integration.

Three concerns, three test groups:

1. :class:`TestStore` — CRUD round-trip against a temp SQLite file:
   save/list/delete/delete_all work and keep the insertion order that
   ``all_examples()`` depends on.

2. :class:`TestFewShotIntegration` — saving a correction invalidates the
   process-wide cached :class:`FewShotIndex` so the next
   ``_get_default_fewshot_index()`` rebuild includes the new pair. The
   BM25 retriever then surfaces the pair for a semantically-similar
   question.

3. :class:`TestHTTPEndpoints` — the four ``/nl-corrections`` endpoints
   proxy the store faithfully and return 400 / 404 on obvious misuse.

Every test runs with an isolated SQLite file (via monkeypatching
``_DB_PATH`` + nulling the cached connection) so the suite stays
hermetic and leaves no ``nl_corrections.db`` on the developer machine.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from arango_cypher import nl_corrections
from arango_cypher.nl2cypher import _core as nl2cypher_core
from arango_cypher.service import app

client = TestClient(app)


@pytest.fixture
def fresh_store(tmp_path, monkeypatch):
    """Point the store at a fresh temp SQLite file for every test.

    Also clears registered invalidation listeners so each test controls
    its own listener set, and resets the FewShotIndex cache so no test
    inherits state from a previous one.
    """
    db_path = tmp_path / "nl_corrections.db"
    monkeypatch.setattr(nl_corrections, "_DB_PATH", str(db_path))
    monkeypatch.setattr(nl_corrections, "_conn", None)
    monkeypatch.setattr(nl_corrections, "_invalidation_listeners", [])
    # Also reset the nl2cypher core cache + listener-registration flag.
    monkeypatch.setattr(nl2cypher_core, "_DEFAULT_FEWSHOT_INDEX", None)
    monkeypatch.setattr(nl2cypher_core, "_DEFAULT_FEWSHOT_RESOLVED", False)
    monkeypatch.setattr(
        nl2cypher_core, "_DEFAULT_FEWSHOT_INVALIDATION_REGISTERED", False
    )
    yield db_path
    # Close the sqlite connection so the temp file can be cleaned up on Windows.
    if nl_corrections._conn is not None:
        nl_corrections._conn.close()
        nl_corrections._conn = None


# ---------------------------------------------------------------------------
# TestStore
# ---------------------------------------------------------------------------


class TestStore:
    def test_save_and_list(self, fresh_store):
        row_id = nl_corrections.save(
            question="How many movies are there?",
            cypher="MATCH (m:Movie) RETURN count(m)",
        )
        assert isinstance(row_id, int) and row_id > 0

        items = nl_corrections.list_all()
        assert len(items) == 1
        assert items[0].question == "How many movies are there?"
        assert items[0].cypher == "MATCH (m:Movie) RETURN count(m)"
        assert items[0].mapping_hash == ""
        assert items[0].created_at

    def test_save_rejects_blank_question_or_cypher(self, fresh_store):
        with pytest.raises(ValueError):
            nl_corrections.save(question="  ", cypher="MATCH (n) RETURN n")
        with pytest.raises(ValueError):
            nl_corrections.save(question="what?", cypher="   ")

    def test_save_with_mapping_records_fingerprint(self, fresh_store):
        mapping = {
            "conceptual_schema": {"entities": [{"label": "Movie"}]},
            "physical_mapping": {"entities": {"Movie": {"collectionName": "movies"}}},
        }
        nl_corrections.save(
            question="List all movies",
            cypher="MATCH (m:Movie) RETURN m",
            mapping=mapping,
        )
        items = nl_corrections.list_all()
        assert len(items) == 1
        assert items[0].mapping_hash  # non-empty, 16 hex chars
        assert len(items[0].mapping_hash) == 16

    def test_all_examples_preserves_insertion_order(self, fresh_store):
        nl_corrections.save(question="Q1", cypher="MATCH () RETURN 1")
        nl_corrections.save(question="Q2", cypher="MATCH () RETURN 2")
        nl_corrections.save(question="Q3", cypher="MATCH () RETURN 3")

        pairs = nl_corrections.all_examples()
        assert pairs == [
            ("Q1", "MATCH () RETURN 1"),
            ("Q2", "MATCH () RETURN 2"),
            ("Q3", "MATCH () RETURN 3"),
        ]

    def test_delete_removes_single_row(self, fresh_store):
        id1 = nl_corrections.save(question="Q1", cypher="C1")
        id2 = nl_corrections.save(question="Q2", cypher="C2")

        assert nl_corrections.delete(id1) is True
        remaining = {c.id for c in nl_corrections.list_all()}
        assert remaining == {id2}

    def test_delete_returns_false_for_missing_id(self, fresh_store):
        assert nl_corrections.delete(9999) is False

    def test_delete_all_returns_count(self, fresh_store):
        nl_corrections.save(question="Q1", cypher="C1")
        nl_corrections.save(question="Q2", cypher="C2")
        assert nl_corrections.delete_all() == 2
        assert nl_corrections.list_all() == []

    def test_delete_all_on_empty_is_zero(self, fresh_store):
        assert nl_corrections.delete_all() == 0


# ---------------------------------------------------------------------------
# TestListeners
# ---------------------------------------------------------------------------


class TestListeners:
    def test_save_fires_listener(self, fresh_store):
        calls: list[int] = []
        nl_corrections.register_invalidation_listener(lambda: calls.append(1))
        nl_corrections.save(question="Q", cypher="MATCH () RETURN 1")
        assert calls == [1]

    def test_delete_fires_listener_only_on_hit(self, fresh_store):
        calls: list[int] = []
        nl_corrections.register_invalidation_listener(lambda: calls.append(1))

        row_id = nl_corrections.save(question="Q", cypher="MATCH () RETURN 1")
        assert calls == [1]

        assert nl_corrections.delete(9999) is False
        assert calls == [1]

        assert nl_corrections.delete(row_id) is True
        assert calls == [1, 1]

    def test_delete_all_fires_listener_only_when_nonempty(self, fresh_store):
        calls: list[int] = []
        nl_corrections.register_invalidation_listener(lambda: calls.append(1))

        assert nl_corrections.delete_all() == 0
        assert calls == []

        nl_corrections.save(question="Q", cypher="MATCH () RETURN 1")
        nl_corrections.delete_all()
        assert calls == [1, 1]  # one for save, one for delete_all

    def test_failing_listener_does_not_break_writes(self, fresh_store):
        def _bad() -> None:
            raise RuntimeError("boom")

        nl_corrections.register_invalidation_listener(_bad)
        row_id = nl_corrections.save(question="Q", cypher="MATCH () RETURN 1")
        assert row_id > 0

    def test_unregister_removes_listener(self, fresh_store):
        calls: list[int] = []

        def _l() -> None:
            calls.append(1)

        nl_corrections.register_invalidation_listener(_l)
        nl_corrections.unregister_invalidation_listener(_l)
        nl_corrections.save(question="Q", cypher="MATCH () RETURN 1")
        assert calls == []


# ---------------------------------------------------------------------------
# TestFewShotIntegration
# ---------------------------------------------------------------------------


class TestFewShotIntegration:
    def test_correction_appears_in_fewshot_index_after_save(self, fresh_store):
        # Cold-start: build the index, note its size.
        index_before = nl2cypher_core._get_default_fewshot_index()
        pytest.importorskip("rank_bm25")  # integration requires BM25
        assert index_before is not None
        size_before = len(index_before.examples)

        nl_corrections.save(
            question="How many actors have won an Oscar?",
            cypher="MATCH (p:Person)-[:WON]->(a:Award) RETURN count(DISTINCT p)",
        )

        index_after = nl2cypher_core._get_default_fewshot_index()
        assert index_after is not None
        assert len(index_after.examples) == size_before + 1

        # The new pair must be present, in insertion-order (appended last).
        last_question, last_cypher = index_after.examples[-1]
        assert last_question == "How many actors have won an Oscar?"
        assert "MATCH (p:Person)-[:WON]->(a:Award)" in last_cypher

    def test_bm25_retrieves_the_correction_for_similar_question(self, fresh_store):
        pytest.importorskip("rank_bm25")

        nl_corrections.save(
            question="How many actors have won an Oscar?",
            cypher="MATCH (p:Person)-[:WON]->(a:Award) RETURN count(DISTINCT p)",
        )

        index = nl2cypher_core._get_default_fewshot_index()
        assert index is not None
        hits = index.retrieve("count of actors with Oscar awards", k=3)
        matched_questions = [q for q, _ in hits]
        assert any("won an Oscar" in q for q in matched_questions), (
            f"BM25 should surface the saved correction; got {matched_questions}"
        )

    def test_delete_triggers_rebuild_so_example_disappears(self, fresh_store):
        pytest.importorskip("rank_bm25")

        row_id = nl_corrections.save(
            question="List very obscure wombat wrangling queries",
            cypher="MATCH (w:Wombat) RETURN w",
        )
        index1 = nl2cypher_core._get_default_fewshot_index()
        assert index1 is not None
        assert any("wombat" in q.lower() for q, _ in index1.examples)

        nl_corrections.delete(row_id)

        index2 = nl2cypher_core._get_default_fewshot_index()
        assert index2 is not None
        assert not any("wombat" in q.lower() for q, _ in index2.examples)


# ---------------------------------------------------------------------------
# TestHTTPEndpoints
# ---------------------------------------------------------------------------


class TestHTTPEndpoints:
    def test_post_list_delete_roundtrip(self, fresh_store):
        resp = client.post(
            "/nl-corrections",
            json={
                "question": "How many movies?",
                "cypher": "MATCH (m:Movie) RETURN count(m)",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "saved"
        assert isinstance(body["id"], int)
        row_id = body["id"]

        listed = client.get("/nl-corrections").json()["corrections"]
        assert len(listed) == 1
        assert listed[0]["question"] == "How many movies?"
        assert listed[0]["cypher"] == "MATCH (m:Movie) RETURN count(m)"

        resp = client.delete(f"/nl-corrections/{row_id}")
        assert resp.status_code == 200
        assert resp.json() == {"status": "deleted"}
        assert client.get("/nl-corrections").json()["corrections"] == []

    def test_post_rejects_empty_question_with_400(self, fresh_store):
        resp = client.post(
            "/nl-corrections",
            json={"question": "   ", "cypher": "MATCH () RETURN 1"},
        )
        assert resp.status_code == 400

    def test_delete_missing_id_returns_404(self, fresh_store):
        resp = client.delete("/nl-corrections/9999")
        assert resp.status_code == 404

    def test_delete_all_clears_store(self, fresh_store):
        client.post(
            "/nl-corrections",
            json={"question": "Q1", "cypher": "MATCH () RETURN 1"},
        )
        client.post(
            "/nl-corrections",
            json={"question": "Q2", "cypher": "MATCH () RETURN 2"},
        )
        resp = client.delete("/nl-corrections")
        assert resp.status_code == 200
        assert resp.json() == {"status": "deleted", "count": 2}
        assert client.get("/nl-corrections").json()["corrections"] == []

    def test_post_fires_listener_registered_via_fewshot_builder(self, fresh_store):
        """End-to-end: HTTP POST → store save → listener → FewShotIndex cache clear."""
        pytest.importorskip("rank_bm25")

        # Warm the cache (which also registers the invalidation listener).
        index_warm = nl2cypher_core._get_default_fewshot_index()
        assert index_warm is not None

        resp = client.post(
            "/nl-corrections",
            json={
                "question": "How many actors have won an Oscar?",
                "cypher": "MATCH (p:Person)-[:WON]->(a:Award) RETURN count(DISTINCT p)",
            },
        )
        assert resp.status_code == 200

        # Cache should have been invalidated; next call rebuilds and includes
        # the new pair.
        index_after = nl2cypher_core._get_default_fewshot_index()
        assert index_after is not None
        assert any(
            "won an Oscar" in q for q, _ in index_after.examples
        ), "HTTP POST should have triggered rebuild with the new correction"
