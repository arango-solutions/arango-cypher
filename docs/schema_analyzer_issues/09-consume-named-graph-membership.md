# Consume named-graph membership from the analyzer

**Labels:** `adoption`, `named-graphs`, `inference-accuracy`

**Status:** consumer-side adopted (back-compat / prefer-embedded-with-fallback).
While the two transpilers adapt, the analyzer is consumed **locally via an
editable/path install** of `arango-schema-mapper` (`0.8.0`, which carries the
named-graph work) — `pip install -e ../arango-schema-mapper` — so we pick up the
changes without a premature release. The committed pin stays `>=0.6.1,<0.7`; bump
it (and retire the happy-path live fallback) once a release is published.

## Background

A single ArangoDB database routinely holds collections from several unrelated
sources — the domain graph we want to query, plus GraphRAG side stores,
analytics result collections, app-control collections (`aga_*`), and our own
schema cache. Users need to **scope** the schema (and therefore translation and
execution) to one or more named graphs so the unrelated collections disappear.

We already supported this **client-side**: `graph_collections(db, name)` reads a
graph's vertex/edge membership live from ArangoDB and `_filter_bundle_to_graph`
prunes the `MappingBundle` to it. That works but costs a live database
round-trip on every scope resolution and duplicates knowledge the analyzer is
now able to emit.

The analyzer's transpiler-integration contract (canonical doc:
`arango-schema-analyzer/docs/transpiler-integration.md`, "Named graphs" section)
now provides:

- **`physicalMapping[*].graphs`** — a per-entry, sorted, multi-valued annotation
  of the named graphs each entity/relationship participates in. Omitted when the
  entry is ungraphed. Rides in CSI's `arangoPhysicalMapping`, so it survives the
  `export_mapping(target="cypher")` export we consume.
- **`metadata.graphMembership`** — a summary block. The analyzer-native shape
  nests per-graph entries under a `graphs` key alongside `status` / `graphCount`
  / `ungraphed` siblings:

  ```json
  {
    "status": "ok",
    "graphCount": 2,
    "graphs": {
      "FinReflectKG": {
        "entities": ["COMP", "..."],
        "relationships": ["discloses", "..."],
        "vertexCollections": ["Node"],
        "edgeCollections": ["relations"]
      }
    },
    "ungraphed": { "entities": ["AgaAuditEvent", "..."], "relationships": ["..."] }
  }
  ```

  In `0.8.0` this rides through the CSI export's `metadata` (we read it directly);
  if a future build drops it from CSI, we reconstruct a flat `{ graphName: {
  vertexCollections, edgeCollections } }` from the per-entry `graphs` tags. Our
  reader (`_graph_membership_collections`) accepts both the nested and flat
  shapes.
- **`analysisOptions.graphScope`** — input for single-graph analysis
  (`INVALID_ARGUMENT` for an unknown graph). Optional for us — we filter the full
  bundle rather than re-analyze per graph.

Membership is **many-to-many** (a shared edge collection such as `relations` can
belong to several graphs) and **partial** (collections in *no* graph — the whole
motivation). Membership is resolved at *collection* granularity and projected
onto every label/type backed by that collection (e.g. all ~20 labels backed by
`Node` inherit the `FinReflectKG` tag).

## Adopted behavior (this repo)

1. **`acquire_mapping_bundle` populates `metadata.graphMembership`.** It lifts the
   summary from the raw analysis metadata when present, otherwise reconstructs it
   from the per-entry `graphs` tags via `_reconstruct_graph_membership`. A
   pre-tagging analyzer build yields an empty summary, which we omit so the
   bundle is correctly treated as *not graph-aware*.

2. **Scoping prefers embedded membership, falls back to the live lookup.**
   `_scope_bundle_to_graph(db, bundle, graph_name)` resolves the graph's
   collections from `metadata.graphMembership` (`_graph_membership_collections`,
   no DB call) when available, else calls `graph_collections(db, name)` exactly
   as before. Both paths funnel through the same `_filter_bundle_to_graph`, so the
   scoped result shape is identical regardless of source. Wired into both the
   request-path read (`read_cached_mapping`) and the force/analyze path
   (`_build_fresh_bundle`).

3. **Unknown graph stays a clean miss/404.** When neither membership nor the live
   lookup can resolve the graph, `graph_collections` raises
   `CoreError(code="UNKNOWN_GRAPH")`; `read_cached_mapping` maps that to `None`
   (→ "pending"), the force path lets it propagate (→ 404).

## Version coupling

Our committed dependency is pinned `arangodb-schema-analyzer>=0.6.1,<0.7`. The
tagging work is **Unreleased atop 0.8.0** upstream. For local development we
install the analyzer editable from the sibling checkout
(`pip install -e ../arango-schema-mapper`), which replaces the published `0.6.1`
with editable `0.8.0` in the venv without touching the committed pin
(`pyproject.toml`: `arangodb-schema-analyzer>=0.6.1,<0.7.0`) — so we don't
prematurely loosen the constraint in version control. The editable install
overrides the pin in the live venv; note that a clean `pip install` of this
project (re-resolving dependencies) would pull `0.6.x` and drop the named-graph
signals, so keep the editable install in place during the adaptation window.

The consumer change above is deliberately back-compatible: with a pre-tagging
build, `graphMembership` is absent and we use the live lookup (byte-identical to
prior behavior, verified live against `FinReflectKG`). With the editable `0.8.0`
build, scoping resolves from embedded membership with no DB call (also verified
live). Once the analyzer ships a release carrying the tags:

1. Bump the pin to the releasing version.
2. Re-acquire mappings (the bundle then carries `graphMembership`); scope
   resolution stops calling `graph_collections` on the happy path.
3. The live `graph_collections` lookup remains as the fallback for the
   heuristic-tier mapping path (analyzer unavailable), so it is **not** retired.

## Follow-ups (separate work packages)

- **Cypher→AQL named-graph traversal.** Emit `GRAPH "<name>"` traversal (and
  disambiguate shared collections) when a session is graph-scoped, instead of
  always going collection-based. Tracked separately — this issue is mapping
  plumbing only.
- **Surface `graphMembership` to the UI** so the graph picker can read membership
  from the bundle rather than calling `/graphs`.
- **`analysisOptions.graphScope` passthrough** on the force path, if we ever want
  the analyzer to scope at source rather than filtering downstream.
