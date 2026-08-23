# Bug Report: Baseline inference fails to extract correct ontology from LPG-style databases

**Component:** `arangodb-schema-analyzer` (baseline / deterministic inference)
**Version:** 0.1.0
**Severity:** High — produces an incorrect ontology and unusable mapping for LPG databases
**Status:** **RESOLVED** (2026-04-11) — analyzer v0.1.0 passes 28/28 acceptance tests. Identical ontology across PG/LPG/hybrid. Per-type properties. Accurate domain/range. Contract v1 with JSON Schema validation. `arango-cypher-py` now routes all schema types through the analyzer as the primary tier.

---

## Summary

The same domain (the Neo4j Movies dataset) can be stored in ArangoDB using three
different physical modeling styles:

| Style  | Database                  | Document collections | Edge collections |
|--------|---------------------------|----------------------|------------------|
| **PG** | `neo4j_movies_pg_test`    | `movies`, `persons`  | `acted_in`, `directed`, `follows`, `produced`, `reviewed`, `wrote` |
| **LPG**| `neo4j_movies_lpg_test`   | `nodes`              | `edges` |
| **Hybrid** | *(to be created)*     | Mix of both          | Mix of both |

All three databases contain the **same data** and represent the **same domain**.
The analyzer should detect the physical model style and then extract the **exact
same ontology** from each, differing only in the physical mapping layer.

**Currently, the analyzer produces three different ontologies depending on the
physical model style, and the LPG output is wrong.**

---

## The expected ontology (identical for all three physical styles)

Regardless of whether the data lives in 8 collections (PG), 2 collections (LPG),
or a mix (hybrid), the conceptual schema should be:

### Entities

| Entity   | Properties                        |
|----------|-----------------------------------|
| `Movie`  | `title`, `released`, `tagline`    |
| `Person` | `name`, `born`                    |

### Relationships

| Relationship | Domain   | Range    | Properties            |
|-------------|----------|----------|-----------------------|
| `ACTED_IN`  | `Person` | `Movie`  | `roles`               |
| `DIRECTED`  | `Person` | `Movie`  |                       |
| `FOLLOWS`   | `Person` | `Person` |                       |
| `PRODUCED`  | `Person` | `Movie`  |                       |
| `REVIEWED`  | `Person` | `Movie`  | `summary`, `rating`   |
| `WROTE`     | `Person` | `Movie`  |                       |

This ontology is a fact about the domain, not about the physical storage. The
analyzer's job is to discover it regardless of the physical model style.

---

## What changes per physical style: the physical mapping only

### PG style (`neo4j_movies_pg_test`)

Each conceptual type maps 1:1 to its own collection:

```json
{
  "entities": {
    "Movie":  { "style": "COLLECTION", "collectionName": "movies" },
    "Person": { "style": "COLLECTION", "collectionName": "persons" }
  },
  "relationships": {
    "ACTED_IN": { "style": "DEDICATED_COLLECTION", "edgeCollectionName": "acted_in" },
    "DIRECTED": { "style": "DEDICATED_COLLECTION", "edgeCollectionName": "directed" }
  }
}
```

### LPG style (`neo4j_movies_lpg_test`)

Multiple conceptual types share a single collection, discriminated by a type field:

```json
{
  "entities": {
    "Movie":  { "style": "LABEL", "collectionName": "nodes", "typeField": "type", "typeValue": "Movie" },
    "Person": { "style": "LABEL", "collectionName": "nodes", "typeField": "type", "typeValue": "Person" }
  },
  "relationships": {
    "ACTED_IN": { "style": "GENERIC_WITH_TYPE", "edgeCollectionName": "edges", "typeField": "relation", "typeValue": "ACTED_IN" },
    "DIRECTED": { "style": "GENERIC_WITH_TYPE", "edgeCollectionName": "edges", "typeField": "relation", "typeValue": "DIRECTED" }
  }
}
```

### Hybrid style

A mix — some types have dedicated collections, others share a collection with a
type discriminator. The physical mapping would contain a mix of `COLLECTION` /
`LABEL` and `DEDICATED_COLLECTION` / `GENERIC_WITH_TYPE` styles.

---

## Actual analyzer output

### PG database (`neo4j_movies_pg_test`) — partially correct

```json
{
  "conceptualSchema": {
    "entities": [
      { "name": "Movy",   "labels": ["Movy"],   "properties": [] },
      { "name": "Person", "labels": ["Person"], "properties": [] }
    ],
    "relationships": [
      { "type": "ACTED_IN", "fromEntity": "Any", "toEntity": "Any", "properties": [] },
      { "type": "DIRECTED", "fromEntity": "Any", "toEntity": "Any", "properties": [] },
      { "type": "FOLLOWS",  "fromEntity": "Any", "toEntity": "Any", "properties": [] },
      { "type": "PRODUCED", "fromEntity": "Any", "toEntity": "Any", "properties": [] },
      { "type": "REVIEWED", "fromEntity": "Any", "toEntity": "Any", "properties": [] },
      { "type": "WROTE",    "fromEntity": "Any", "toEntity": "Any", "properties": [] }
    ]
  },
  "physicalMapping": {
    "entities": {
      "Movy":   { "style": "COLLECTION", "collectionName": "movies" },
      "Person": { "style": "COLLECTION", "collectionName": "persons" }
    },
    "relationships": {
      "ACTED_IN": { "style": "DEDICATED_COLLECTION", "edgeCollectionName": "acted_in" }
    }
  }
}
```

**Issues:**

- `"Movy"` instead of `"Movie"` — the singularization of `movies` is wrong
  (the `ies→y` rule is incorrectly applied to words ending in `-vies`).
- All `fromEntity` / `toEntity` are `"Any"` — no domain/range inference despite
  `_from`/`_to` fields being available in every edge document.
- All properties are empty — no field sampling performed.

### LPG database (`neo4j_movies_lpg_test`) — completely wrong

```json
{
  "conceptualSchema": {
    "entities": [
      { "name": "Node", "labels": ["Node"], "properties": [] }
    ],
    "relationships": [
      { "type": "EDGES", "fromEntity": "Any", "toEntity": "Any", "properties": [] }
    ]
  },
  "physicalMapping": {
    "entities": {
      "Node": { "style": "COLLECTION", "collectionName": "nodes" }
    },
    "relationships": {
      "EDGES": { "style": "DEDICATED_COLLECTION", "edgeCollectionName": "edges" }
    }
  }
}
```

**Issues:**

- The entire ontology is wrong. Instead of `Movie` and `Person`, it reports a
  single entity `Node`. Instead of 6 relationship types, it reports a single
  `EDGES`.
- No LPG pattern detected — `detectedPatterns` is empty.
- Physical mapping uses `COLLECTION` / `DEDICATED_COLLECTION` instead of
  `LABEL` / `GENERIC_WITH_TYPE`.
- The `type` discriminator field in `nodes` and the `relation` discriminator
  field in `edges` are completely ignored.

---

## Root cause

The baseline inference appears to apply a fixed algorithm:

1. One document collection → one entity (singularized collection name)
2. One edge collection → one relationship (uppercased collection name)

This works only for PG-style databases. It does not inspect document contents
for type discriminator fields, so LPG and hybrid databases produce incorrect
ontologies.

The correct approach is:

1. **Detect the physical model style first** — sample documents and check for
   discriminator fields (`type`, `_type`, `label`, `labels`, `kind`,
   `entityType` for nodes; `type`, `relation`, `relType`, `_type` for edges).
   Use `COLLECT` (not just `LIMIT`-based sampling) to find all distinct values,
   since documents may be ordered by type.

2. **Extract the ontology from the discriminator values** — each distinct value
   of the type field becomes an entity or relationship type in the conceptual
   schema.

3. **Build the physical mapping according to the detected style** — `LABEL` /
   `GENERIC_WITH_TYPE` for LPG, `COLLECTION` / `DEDICATED_COLLECTION` for PG.

4. **Infer domain/range** — for each relationship type (whether a dedicated edge
   collection or a discriminated subset of a shared collection), sample the
   `_from`/`_to` fields to determine which entity types participate.

5. **Sample properties per conceptual type** — filter documents by their type
   value before collecting field names. Different entity types in the same
   collection may have completely different fields (e.g., Movie has `title`,
   `released`, `tagline`; Person has `name`, `born`).

---

## Physical model details for reproduction

### PG: `neo4j_movies_pg_test`

```
movies     (document, 39 docs)   — {title, released, tagline}
persons    (document, 134 docs)  — {name, born}
acted_in   (edge, 175 docs)      — {roles}             persons/* → movies/*
directed   (edge, 45 docs)       — {}                   persons/* → movies/*
follows    (edge, 3 docs)        — {}                   persons/* → persons/*
produced   (edge, 15 docs)       — {}                   persons/* → movies/*
reviewed   (edge, 9 docs)        — {summary, rating}    persons/* → movies/*
wrote      (edge, 10 docs)       — {}                   persons/* → movies/*
```

### LPG: `neo4j_movies_lpg_test`

```
nodes  (document, 173 docs)
  type="Movie"  (39 docs)   — {title, released, tagline}
  type="Person" (134 docs)  — {name, born}

edges  (edge, 257 docs)
  relation="ACTED_IN"  (175 docs) — {roles}    Person → Movie
  relation="DIRECTED"  (45 docs)  — {}         Person → Movie
  relation="FOLLOWS"   (3 docs)   — {}         Person → Person
  relation="PRODUCED"  (15 docs)  — {}         Person → Movie
  relation="REVIEWED"  (9 docs)   — {summary, rating}  Person → Movie
  relation="WROTE"     (10 docs)  — {}         Person → Movie
```

Sample documents:

```json
// nodes — Movie
{ "_key": "TheMatrix", "type": "Movie", "labels": ["Movie"],
  "title": "The Matrix", "released": 1999, "tagline": "Welcome to the Real World" }

// nodes — Person
{ "_key": "Keanu", "type": "Person", "labels": ["Person"],
  "name": "Keanu Reeves", "born": 1964 }

// edges — ACTED_IN
{ "_from": "nodes/Keanu", "_to": "nodes/TheMatrix",
  "relation": "ACTED_IN", "roles": ["Neo"] }

// edges — REVIEWED
{ "_from": "nodes/JessicaThompson", "_to": "nodes/CloudAtlas",
  "relation": "REVIEWED", "summary": "An amazing journey", "rating": 95 }
```

---

## Test script

```python
from arango import ArangoClient
from schema_analyzer import AgenticSchemaAnalyzer, export_mapping

client = ArangoClient(hosts="http://localhost:28529")
analyzer = AgenticSchemaAnalyzer()

for db_name in ["neo4j_movies_pg_test", "neo4j_movies_lpg_test"]:
    db = client.db(db_name, username="root", password="openSesame")
    result = analyzer.analyze_physical_schema(db)
    export = export_mapping(
        {
            "conceptualSchema": result.conceptual_schema,
            "physicalMapping": result.physical_mapping,
            "metadata": result.metadata.model_dump(by_alias=True),
        },
        target="cypher",
    )
    print(f"\n--- {db_name} ---")
    print(f"Entities: {[e['name'] for e in export['conceptualSchema']['entities']]}")
    print(f"Relationships: {[r['type'] for r in export['conceptualSchema']['relationships']]}")

# Expected: both databases should produce the exact same entity and relationship lists:
#   Entities: ['Movie', 'Person']
#   Relationships: ['ACTED_IN', 'DIRECTED', 'FOLLOWS', 'PRODUCED', 'REVIEWED', 'WROTE']
```

---

## Impact

Without correct LPG detection, any database using shared node/edge collections
with type discriminators (the standard Neo4j-to-ArangoDB migration pattern)
produces an incorrect ontology. Downstream consumers like `arango-cypher-py`
cannot generate valid Cypher-to-AQL translations from the resulting mapping.

We have implemented a workaround in `arango-cypher-py` (`schema_acquire.py`)
that classifies the physical model style, extracts the correct ontology from all
three styles, and builds the appropriate physical mapping. Happy to share the
implementation or collaborate on upstreaming the logic.

---

## Follow-up: Hybrid edge collections still misclassified (2026-04-11)

**Status:** **RESOLVED** (2026-04-14) — `_pick_best_type_field` now accepts single-value edge discriminators when the value differs from the collection-name-derived type. 4 new unit tests added. `arango-cypher-py` workaround (`_fixup_dedicated_edges`) can be removed.

### Problem

When an ArangoDB database uses a **hybrid** physical model — some document
collections are PG-style (one entity per collection) and some are LPG-style
(shared collection with type discriminator) — the analyzer correctly detects
entity types in shared document collections (`LABEL` style) but **fails to
detect type discriminators in shared edge collections**.

Specifically, for the `cypher_hybrid_fixture` database:

| Collection | Type | Contents |
|-----------|------|----------|
| `users` | document | User entities (PG-style, one-per-collection) |
| `vertices` | document | Doc, Note entities (LPG-style, `type` discriminator) |
| `edges` | edge | FOLLOWS relationships (LPG-style, `type` discriminator) |

The `edges` collection has a `type` field with value `"FOLLOWS"` on every edge.

### Expected output

```json
{
  "relationships": {
    "FOLLOWS": {
      "style": "GENERIC_WITH_TYPE",
      "edgeCollectionName": "edges",
      "typeField": "type",
      "typeValue": "FOLLOWS",
      "domain": "User",
      "range": "User"
    }
  }
}
```

### Actual output

```json
{
  "relationships": {
    "EDGES": {
      "style": "DEDICATED_COLLECTION",
      "edgeCollectionName": "edges",
      "domain": "User",
      "range": "User"
    }
  }
}
```

The analyzer treats the `edges` collection as a dedicated collection for a
single relationship type named `"EDGES"` (the uppercased collection name),
ignoring the `type` discriminator field entirely.

### Workaround

`arango-cypher-py` now applies a `_fixup_dedicated_edges` post-processor after
the analyzer returns. For each `DEDICATED_COLLECTION` relationship, it queries
the actual edge collection for a type discriminator field (`type`, `relation`,
`relType`, `_type`, `label`). If one is found with distinct values, it replaces
the single entry with per-type `GENERIC_WITH_TYPE` entries and infers
domain/range from `_from`/`_to` sampling.

### Suggested fix

The analyzer's baseline inference should apply the same discriminator detection
logic to edge collections that it already applies to document collections.
Specifically:

1. For each edge collection, check if a discriminator field exists (same
   candidates: `type`, `relation`, `relType`, `_type`).
2. If found, use `COLLECT DISTINCT` to enumerate all values.
3. Emit one `GENERIC_WITH_TYPE` relationship per distinct value instead of a
   single `DEDICATED_COLLECTION`.
4. Sample `_from`/`_to` per type value to infer domain/range.

### Reproduction

```python
from arango import ArangoClient
from schema_analyzer import AgenticSchemaAnalyzer, export_mapping

client = ArangoClient(hosts="http://localhost:28529")
db = client.db("cypher_hybrid_fixture", username="root", password="openSesame")

analyzer = AgenticSchemaAnalyzer()
result = analyzer.analyze_physical_schema(db)
export = export_mapping(
    {
        "conceptualSchema": result.conceptual_schema,
        "physicalMapping": result.physical_mapping,
        "metadata": result.metadata.model_dump(by_alias=True),
    },
    target="cypher",
)

rels = export["conceptualSchema"]["relationships"]
print(f"Relationship types: {[r['type'] for r in rels]}")
# Actual:   ['EDGES']
# Expected: ['FOLLOWS']

pm_rels = export["physicalMapping"]["relationships"]
for k, v in pm_rels.items():
    print(f"  {k}: style={v.get('style')}")
# Actual:   EDGES: style=DEDICATED_COLLECTION
# Expected: FOLLOWS: style=GENERIC_WITH_TYPE
```
