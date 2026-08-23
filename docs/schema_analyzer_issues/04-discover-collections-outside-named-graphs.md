# Guarantee every snapshot collection is represented in the exported mapping

**Labels:** `enhancement`, `inference-accuracy`, `llm-path`

## Background

`snapshot.py::snapshot_physical_schema` correctly enumerates every non-system collection returned by `db.collections()` and captures each one in `snapshot["collections"]`. The baseline inference path (`infer_baseline_from_snapshot`) then iterates that list and emits an entry per collection, so the baseline path is complete by construction.

The LLM path does not have the same guarantee. `AgenticSchemaAnalyzer.analyze_physical_schema` (`analyzer.py:335`) passes the snapshot into `run_generate_validate_repair` which prompts an LLM to produce the `conceptualSchema` + `physicalMapping`. The LLM is free to omit collections — and in practice it does, particularly:

- When a collection is not referenced by any named graph's `edge_definitions` or `orphan_collections` (the LLM treats named-graph participation as a salience signal).
- When a collection has a name that looks like a staging / intermediate table (`*_temp`, `*_archive`).
- When token budgets force the LLM to trim what it considers "less important" collections.

None of these omissions are visible to the caller — they just silently disappear from the export.

`arango-cypher-py` works around this with `_backfill_missing_collections` (~160 LOC in `arango_cypher/schema_acquire.py:841`). That function diffs the DB's collection list against the export's entities and relationships, and for every gap it re-runs the heuristic inference locally. This is exactly the kind of duplicated logic the no-workaround policy is meant to prevent.

## Current behavior

LLM path, simplified:

```python
snapshot = snapshot_physical_schema(db)  # complete: every collection
data = run_generate_validate_repair(...)  # may drop collections
return AnalysisResult(
    conceptual_schema=data["conceptualSchema"],
    physical_mapping=data["physicalMapping"],
    metadata={...},
)
```

There is no reconciliation step comparing `snapshot["collections"]` against the collections referenced by `data["physicalMapping"]`.

## Desired behavior

Add a completeness-reconciliation step between the LLM output and the `AnalysisResult`. Preferred shape:

1. After `run_generate_validate_repair` returns, collect the set of collection names referenced by the LLM's `physicalMapping` — union of `entities[*].collectionName` and `relationships[*].edgeCollectionName` (or `collectionName` for relationships, per Issue 05 naming alignment).
2. Compute `missing = snapshot_collection_names - llm_referenced_names`.
3. For each missing collection, run the baseline inference *for that single collection* and merge the result into the physical mapping / conceptual schema.
4. Record the reconciliation in `AnalysisMetadata` so downstream consumers and evals can see how often the LLM path is under-covering:
   ```json
   {
     "reconciliation": {
       "llm_covered_collections": 18,
       "snapshot_collections": 22,
       "backfilled_collections": ["audit_log", "session_cache", "migrations", "feature_flags"],
       "strategy": "baseline_per_missing_collection"
     }
   }
   ```
5. Add a corresponding `AnalysisMetadata.warnings` entry so the `tool_contract` response surfaces it as a non-fatal warning.

Design alternative considered and rejected: add the LLM a "every collection must appear" constraint in the validate-repair loop. Rejected because it burns additional LLM tokens on the most predictable part of the task (the deterministic baseline handles known collections perfectly well), and because a repair loop has no hard guarantee of convergence within the `max_repair_attempts` budget.

## Acceptance criteria

1. Live database with 10 non-system collections where the LLM (stubbed to return 7) emits only 7 of them — after reconciliation the returned `physicalMapping` has entries for all 10.
2. `AnalysisMetadata.reconciliation.backfilled_collections` correctly lists the 3 that were backfilled.
3. Backfilled collections use baseline classification (not a degraded "unknown" placeholder): `COLLECTION` / `LABEL` / `DEDICATED_COLLECTION` / `GENERIC_WITH_TYPE` per the same heuristics the baseline path uses.
4. No-op when the LLM output already covers every snapshot collection (no metadata noise, no empty `backfilled_collections` list — omit the key).
5. Baseline-only runs (LLM disabled / unavailable) are unaffected — reconciliation is only relevant to the LLM path.
6. Coverage in `tests/test_analyzer.py` with a mocked `run_generate_validate_repair` that returns a deliberately incomplete physical mapping.

## Non-goals

- No change to the snapshot phase. The snapshot is already complete.
- No change to the baseline path. Baseline is already complete by construction.
- No change to LLM prompting or the validate-repair loop. The fix is purely post-processing.

## Impact on downstream consumers

Removes `_backfill_missing_collections` and the duplicated `_props_to_pm` helper from `arango-cypher-py/arango_cypher/schema_acquire.py` — ~160 LOC of workaround plus one `logger.info("Backfilling %d missing document and %d missing edge collections", ...)` that should really be an analyzer-internal concern.

More importantly: it closes a silent correctness bug. Today, any downstream that asks "does the mapping cover my whole DB?" gets `yes` — but only because `arango-cypher-py` backfills. Every other consumer of the analyzer (MCP server, direct CLI use, tool-contract callers) gets the incomplete LLM output with no warning. This reconciliation step makes completeness a property of the analyzer itself.

## Implementation notes

The baseline inference in `baseline.py` is already structured as "for each collection, emit an entry." Exposing a function that takes a single snapshot collection and returns `(entity_entry_or_rel_entry, conceptual_addition)` lets the reconciliation step reuse it. Rough shape:

```python
# In baseline.py:
def infer_mapping_for_collection(
    snapshot_entry: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Baseline inference for a single collection.
    Returns (physical_mapping_entry, conceptual_schema_entry)."""
    ...


# In analyzer.py, after run_generate_validate_repair returns `data`:
covered = _referenced_collections(data["physicalMapping"])
missing = [e for e in snapshot["collections"] if e["name"] not in covered]
for entry in missing:
    pm_entry, cs_entry = infer_mapping_for_collection(entry)
    _merge_into(data, pm_entry, cs_entry)
data["metadata"]["reconciliation"] = {...}
```

About ~50 LOC of new code in the analyzer, total.
