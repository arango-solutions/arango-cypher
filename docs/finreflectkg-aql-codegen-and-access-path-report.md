# Bug Report / Feature Request: single-hop AQL codegen — vertex-centric access path, multi-`WITH` invalid AQL, and index advisories

**Component:** `arango-cypher-py` / `arango-query-core` — AQL code generation (`translate`)
and the `nl2cypher` front-end (retry hint + `IndexAdvisory`)
**Version:** `arango-cypher-py` 0.1.0 (editable, `main`) · `arango-query-core` 0.1.0 ·
`arangodb-schema-analyzer` 0.9.0
**Reporter:** FinReflectKG POC (17.5 M-edge financial knowledge graph, ArangoDB 3.12.x
Enterprise cluster: 3 Coordinators / 3 DBServers / 3 Agents)
**Severity:** High for query performance at scale — after the vocabulary gap was fixed
(see `finreflectkg-cypher-vocabulary-bug-report.md`), these are the remaining ceiling
on the 22-query gold set: 10 transpiled queries are **killed at the runtime cap** and
2–3 produce **invalid AQL**.
**Status:** OPEN — three findings, all reproduced with EXPLAIN evidence below.

---

## Context

FinReflectKG models a labeled property graph as a single `Node` document collection and
a single `relations` edge collection. Edges carry `type`, `_fromType`, `_toType`, and the
graph is indexed with **vertex-centric indexes (VCIs)**:

- `relations(_from, type, _toType)`  → `vci_from_type_totype`
- `relations(_to, type, _fromType)`  → `vci_to_type_fromtype`

A **verified property of this 3.12.x cluster** (independently reproduced for this report):
the VCIs are engaged by **direct edge-collection queries**
(`FOR e IN relations FILTER e._from == @x AND e.type == … AND e._toType == …`) but **not**
by pattern traversals (`FOR v,e IN 1..1 OUTBOUND … relations`). The optimizer uses the
built-in `edge` index for traversals and applies `type`/`_toType` as **post-filters** —
and an explicit `indexHint` with `forceIndexHint: true` does **not** change this (evidence
in Finding 1). This is the crux of Findings 1 and 3.

The gold set and full results live in the FinReflectKG repo
(`docs/nl-graphrag.md`, `docs/cypher-queries.md`); after driving translation through the
schema-aware `nl_to_cypher` front-end the vocabulary failures are gone (19/22 transpile,
0 `MAPPING_NOT_FOUND`), leaving the three codegen issues below.

---

## Finding 1 — typed single-hop `MATCH` emits a traversal that cannot use a vertex-centric index

**Symptom.** Every typed single-hop pattern `(a)-[:REL]->(b:Label)` transpiles to an
`OUTBOUND` traversal with the type/label constraints as post-filters. On a bound start
node this is merely sub-optimal; on a label-rooted pattern it is fatal (full label scan
× full edge fan-out → killed at the runtime cap).

**Reproduction — the AQL `translate` currently emits** (gold #5,
"organizations operating in > 3 locations"):

```aql
WITH Node
FOR o IN @@uCollection
  FILTER o[@uTypeField] == @uTypeValue                 -- all 22,640 :ORG nodes
  FOR g, r IN 1..1 OUTBOUND o @@edgeCollection          -- expand EVERY edge of each
    FILTER IS_SAME_COLLECTION(@vCollection, g)
    FILTER g[@vTypeField] == @vTypeValue                 -- _toType post-filter
    FILTER r[@relTypeField] == @relTypeValue             -- type post-filter
    COLLECT o_1 = o AGGREGATE locationCount = COUNT(g), …
```

**EXPLAIN evidence (this cluster):**

| Query form | est. cost | index used |
|---|---:|---|
| bound-start 1-hop, **traversal** (as emitted) | 127 | `edge` (post-filters `type`/`_toType`) |
| bound-start 1-hop, **direct edge query** | **59** | **`vci_from_type_totype`** |
| traversal **+ `forceIndexHint` → `vci_from_type_totype`** | 253 | `edge` (hint ignored) |
| label-rooted agg, **traversal** (as emitted) | — | times out (all-ORG fan-out) |
| label-rooted agg, **direct edge query + type index** | **465** | type-leading index → **1.9 s** (was: killed at 90 s) |

**Root cause.** The codegen lowers a fixed single hop to a traversal. Traversals on
3.12.x can't be steered onto a `(_from, type, _toType)` persistent index (confirmed:
`forceIndexHint` is ignored), so the only access path that engages the VCI is a direct
edge-collection query — which the codegen never emits and offers no option to request
(`translate()` exposes no access-path/index knob).

**Proposed fix.** For a **fixed-length single hop** `(a)-[:REL]->(b)` (optionally with
label filters), emit a **direct edge-collection query** against the edge collection:

```aql
FOR e IN relations
  FILTER e._from == <a> AND e.type == <REL> [AND e._toType == <Blabel>]
  LET b = DOCUMENT(e._to)
  …
```

and reserve `OUTBOUND`/`INBOUND` traversals for variable-length paths (`*`, `1..n`).
This is the shape that engages `vci_from_type_totype` / `vci_to_type_fromtype` (and, for
label-rooted aggregations, a `(type, _fromType, _toType)` index — see Finding 3). Even a
`translate` option (e.g. `single_hop_as_edge_query=True`) would unblock it.

---

## Finding 2 — carrying a variable across `WITH` into a later `MATCH` emits invalid AQL (ERR 1511)

**Symptom.** A multi-`WITH` query that rebinds a node variable across `MATCH` clauses
transpiles, but the AQL fails to parse: `variable 'o' is assigned multiple times`
(HTTP 400, ERR 1511). This is a **correctness** defect — invalid AQL, not slow AQL — and
it is **deterministic codegen, independent of the LLM**: the input Cypher is valid.

**Reproduction (both transpile; only the AQL differs):**

```cypher
-- V1 (as the front-end naturally phrases it) → INVALID AQL (ERR 1511)
MATCH (o:ORG)-[:discloses]->(f:FINMETRIC)
WITH o, count(f) AS metrics_count
MATCH (r:ORG)-[:regulates]->(o)
WITH o, metrics_count, count(r) AS regulators_count
WHERE metrics_count > 50 AND regulators_count > 3
RETURN o, metrics_count ORDER BY metrics_count DESC LIMIT 1

-- V2 (same semantics, one connected MATCH) → VALID AQL, passes EXPLAIN
MATCH (r:ORG)-[:regulates]->(o:ORG)-[:discloses]->(f:FINMETRIC)
WITH o, count(DISTINCT f) AS metrics_count, count(DISTINCT r) AS regulators_count
WHERE metrics_count > 50 AND regulators_count > 3
RETURN o, metrics_count ORDER BY metrics_count DESC LIMIT 1
```

`translate(V1)` → AQL that reassigns `o` (server: `ERR 1511 variable 'o' is assigned
multiple times`). `translate(V2)` → AQL that validates. Same failure class as
hand-written gold #16 (3-hop `variable 'v' assigned multiple times`).

**Root cause.** When a variable bound in one `MATCH` is projected through a `WITH` and
re-referenced in a subsequent `MATCH`, codegen re-declares the AQL `FOR`/`LET` variable
for that name instead of reusing the projected binding (the `COLLECT`/subquery output),
so the same AQL variable is assigned twice.

**Proposed fix.** Reuse the `WITH`-projected binding for a carried variable rather than
emitting a new `FOR`/`LET` that shadows it; equivalently, alpha-rename the re-matched
occurrence and join on `_id` identity. (A more capable LLM is **not** the fix — it only
avoids the pattern by chance; the transpiler must accept valid multi-`WITH` Cypher.)

---

## Finding 3 — extend `IndexAdvisory` to typed-hop access paths, and sharpen the ERR 1511 retry hint

Two smaller, high-leverage items that make the above self-evident to callers:

**(a) Index advisories for typed hops.** The library already ships
`nl2cypher.IndexAdvisory(collection, field, reason)` and surfaces `NL2CypherResult.advisories`,
but only for the **fuzzy name-match / ArangoSearch** case. The transpiler knows both the
query shape *and* the acquired mapping's indexes, so it is well-positioned to advise on
typed hops too — e.g. "single hop `(a)-[:REL]->(b)` filters on `type`/`_toType` but the
emitted traversal can't use `vci_from_type_totype`; use a direct edge query," or
"label-rooted aggregation over `:ORG` has no `type`-leading index — a
`(type, _fromType, _toType)` index would prune it." The library should **advise**; the DB
owner **provisions** (a translation library should not run DDL against the user's schema).

**(b) Fix the ERR 1511 retry hint.** The `nl2cypher` validation-retry currently emits:
*"the variable `o` is bound more than once … a path variable (`MATCH o = …`) was reused."*
For the Finding-2 case this is a **misdiagnosis** — no path variable was used — so the
same-model retry can't act on it. A hint like *"do not carry a node variable across a
`WITH` into a new `MATCH`; express both patterns as one connected `MATCH`"* lets the
existing model self-correct (V1 → V2) without escalating to a larger model.

---

## FinReflectKG-side mitigation already applied

To unblock label-rooted aggregations (Finding 1, label-rooted case) for hand-written AQL,
the benchmark suite, and GraphRAG retrieval, FinReflectKG added two `type`-leading
persistent indexes on `relations`:

- `vci_type_fromtype_totype` → `(type, _fromType, _toType)`
- `vci_type_totype_fromtype` → `(type, _toType, _fromType)`

Measured: the `operates_in`/`ORG`/`GPE` filter matches 313,407 of 17,513,372 edges
(1.79%); the direct-edge form's estimated cost drops **119,966,595 → 465** and it runs in
**~1.9 s** (previously killed at the 90 s cap). These indexes only help once the
transpiler emits **direct edge queries** (Finding 1) — the current traversal output cannot
use them — which is why Findings 1 and 3(a) are the upstream half of the fix.
