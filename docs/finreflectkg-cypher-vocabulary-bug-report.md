# Bug Report / Feature Request: Cypher label & relationship-type resolution is exact-match only (vocabulary bridging should live in the transpiler)

**Component:** `arango-cypher-py` — `arango_query_core.mapping.MappingResolver`
(+ schema acquisition via `arango_cypher.schema_acquire` / `arangodb-schema-analyzer`)
**Version:** `arango-cypher-py` 0.1.0 · `arangodb-schema-analyzer` 0.9.x (analyzer extra)
**Reporter:** FinReflectKG POC (17.5 M-edge financial knowledge graph on ArangoDB 3.12.x Enterprise)
**Severity:** High for the "port existing Cypher" / hand-written-Cypher use case — 19/22 real-world
Cypher queries fail to transpile against a correctly-acquired mapping, all for vocabulary reasons.
**Status:** PARTIALLY RESOLVED (2026-07-07) — see **Resolution** below.

---

## Resolution (2026-07-07) — where each fix belongs

The 19 failures split across three owners; the largest share (17/19) is fixed in
`arango-cypher-py` by the change below.

| Root cause | Owner | Status |
|---|---|---|
| #1 Resolver exact-match (case / `_-` / lemma) | **arango-cypher-py** (`arango_query_core.mapping.MappingResolver`) | **FIXED** — normalized resolution |
| #2 Lossy label rename `FIN_METRIC`→`FINMETRIC` | root cause in **arangodb-schema-analyzer** (`export_mapping`); **worked around** here | **RESOLVED here** via normalization; analyzer should still preserve label fidelity |
| #3 Top-20 entity cap drops `ORG_REG` | **arangodb-schema-analyzer** (`export_mapping` applies the entity cap; `arango-cypher-py/schema_acquire` only caps *relationships*) | **UPSTREAM** — needs a configurable/transparent entity cap in the analyzer |
| #4 `reduce(...)` unsupported | **arango-cypher-py** (transpiler grammar/compiler) | **FIXED (partial)** — grammar + sum-fold lowering; non-sum folds get a clear capability error |

**Fix landed here:** `MappingResolver.resolve_entity` / `resolve_relationship` now fall back to a
**case- and separator-insensitive** match (build a normalized `casefold` + strip-`_-\s` index, exact
match first, ambiguous collision → `AMBIGUOUS_MAPPING`). This resolves both the Neo4j-vocabulary
mismatch (`Has_Stake_In` → `has_stake_in`, 11 failures) **and** the analyzer's lossy rename
(`FIN_METRIC`/`RISK_FACTOR` → `FINMETRIC`/`RISKFACTOR`, 6 of the 7 entity failures) — because the
normalized forms are equal. Lemmatisation (plural/singular) is intentionally not applied (ambiguous).
Tests: `tests/test_mapping_resolver_normalization.py`.

**Still upstream (analyzer):** `ORG_REG` is *absent* from the mapping (dropped by the analyzer's
top-N entity cap), so no resolver normalization can recover it — the analyzer needs a configurable /
transparent entity cap (and, ideally, label fidelity for #2 so the raw `type` value stays an accepted
key). Filed as `arango-schema-mapper/docs/cypher-vocabulary-fidelity-bug-report.md`.

**`reduce(...)` (#4):** now parses (grammar rule + regenerated parser) and the common numeric
**sum-fold** (`reduce(acc = init, x IN list | acc + f(x))`) lowers to
`(init + SUM((FOR x IN list RETURN f(x))))`; other accumulations (`*`, string concat, etc.) raise a
clear `NOT_IMPLEMENTED` capability error instead of a cryptic grammar syntax error (AQL has no general
fold). Tests: `tests/test_translate_reduce.py`.

**Net:** 17 of the 19 failures are addressed by the resolver fix, plus `reduce(...)` sum-folds now
translate; only `ORG_REG` (analyzer entity cap, filed upstream) genuinely remains.

---

---

## Summary

When transpiling Cypher against a live-acquired `MappingBundle`, `MappingResolver.resolve_entity`
and `resolve_relationship` do a **plain exact-key dict lookup**. Any label or relationship type
whose spelling differs from the mapping key — by **case**, **lemmatization**, **underscores**, or
because the analyzer **renamed** the label — raises `MAPPING_NOT_FOUND`, even when the entity/edge
plainly exists in the graph.

The consequence is that the **vocabulary-bridging responsibility falls on the caller**: to transpile
successfully a user must first discover the analyzer's exact, normalized label spellings and rewrite
their Cypher to match. For a Cypher→AQL transpiler whose whole value proposition is running Cypher
over an ArangoDB LPG, that bridging should live **inside the library** (resolver and/or the
schema-aware `nl2cypher` front-end), not in every caller.

### Evidence (FinReflectKG)

Graph: one `Node` document collection (type-discriminated: `ORG`, `FIN_METRIC`, `GPE`, `ORG_REG`, …)
and one `relations` edge collection (type-discriminated: `discloses`, `has_stake_in`, `operates_in`, …).
Mapping acquired with `get_mapping(db, graph_name="FinReflectKG")` → **20 entities, 200 relationship types**.

Transpiling the 22 curated gold Cypher queries (the original openCypher, as ported from Neo4j):
**3/22 transpile + execute**; the other 19 fail:

| Failure | Count | Example |
|---|---|---|
| `No relationship mapping for: <Type>` | 11 | `Has_Stake_In`, `Operates_In`, `Depends_On`, `Negatively_Impacts` — the graph has `has_stake_in`, `operates_in`, … |
| `No entity mapping for: <Label>` | 7 | `:FIN_METRIC` (mapping key is `FINMETRIC`), `:ORG_REG` (dropped by the top-20 cap), `:RISK_FACTOR` (`RISKFACTOR`) |
| `CYPHER_SYNTAX_ERROR` | 1 | query uses `reduce(...)` — unsupported construct |

---

## Root causes

### 1. Resolver is exact-match, case-sensitive, no normalization/alias

```python
# arango_query_core/mapping.py  (MappingResolver)
def resolve_entity(self, label_or_entity: str) -> JsonObj:
    entities = pm.get("entities") ...
    mapping = entities.get(label_or_entity)          # <-- exact dict key
    if not isinstance(mapping, dict):
        raise CoreError(f"No entity mapping for: {label_or_entity}...", code="MAPPING_NOT_FOUND")

def resolve_relationship(self, rel_type: str) -> JsonObj:
    rels = pm.get("relationships") ...
    mapping = rels.get(rel_type)                     # <-- exact dict key
    if not isinstance(mapping, dict):
        raise CoreError(f"No relationship mapping for: {rel_type}...", code="MAPPING_NOT_FOUND")
```

`Has_Stake_In` ≠ `has_stake_in`, so it misses. There is no case-fold, no separator/lemma
normalization, and no synonym/alias mechanism.

### 2. Lossy entity-label normalization in the analyzer export

The actual node `type` value in the data is `FIN_METRIC`, but the acquired mapping exposes the
label **`FINMETRIC`** (underscores stripped, upper-cased). So a Cypher author who inspects the data
and writes the *real* type value `:FIN_METRIC` **still** fails — the mapping key no longer equals any
value that appears in the graph. (`RISK_FACTOR`→`RISKFACTOR`, `ECON_IND`→`ECONIND`, etc.)

### 3. Entity label set is capped (top-N by volume) with no fallback

The mapping carries only the **top 20** entity labels by volume. `ORG_REG` (~11 K nodes, rank ~24) is
absent, so any query referencing it fails `MAPPING_NOT_FOUND` even with a perfectly-spelled label.
(Relationships are capped at `DEFAULT_MAX_RELATIONSHIP_TYPES = 200`, which was sufficient here.)

### 4. Cypher coverage gap: `reduce(...)`

`CYPHER_SYNTAX_ERROR ... no viable alternative at input 'reduce'`.

---

## Expected behavior

A Cypher label / relationship type should resolve to the graph's mapping when it refers to the same
thing, regardless of case/underscores/lemma, and regardless of the analyzer's internal label
spelling. The transpiler owns the conceptual↔physical vocabulary bridge; callers should not have to
reverse-engineer normalized label spellings.

---

## Proposed fixes (in priority order)

1. **Normalized resolution in `MappingResolver`.** Build a normalized index of mapping keys
   (e.g. `casefold`, strip `_-`, optional lemma) and match the incoming label/type against it;
   fall back to exact. Ambiguous collisions → a structured error listing candidates. This alone
   fixes causes #1 and #2.
2. **Alias/synonym support.** Let a `MappingBundle` (or a `translate()` option) carry
   `aliases: {cypherLabel -> mappingKey}` so a project can pin e.g. `Has_Stake_In -> has_stake_in`
   deterministically when auto-normalization is undesirable.
3. **Label fidelity in the analyzer export.** Preserve the raw `type` value as an accepted key/alias
   for LPG entities (so `:FIN_METRIC` resolves even when the canonical label is `FINMETRIC`), or stop
   the lossy rename.
4. **Configurable / transparent entity cap.** Make the top-N entity cap configurable and, when a
   referenced label falls outside it, resolve it live (or emit a warning naming dropped labels) rather
   than a flat `MAPPING_NOT_FOUND`.
5. **Cypher coverage.** Support `reduce(...)`, or emit a clear "unsupported construct" capability
   error distinct from a vocabulary miss.
6. **Docs.** State that hand-written Cypher must use mapping labels, and that the schema-aware
   `nl2cypher` front-end is the recommended path (it can emit mapping-correct labels and sidestep #1–#3).

---

## Reproduction

```bash
# In a py3.11 env with:  pip install -e '.[analyzer]'
python - <<'PY'
from arango import ArangoClient
from arango_cypher import translate
from arango_cypher.schema_acquire import get_mapping

db = ArangoClient(hosts="https://<endpoint>").db("FinReflectKG", username="...", password="...")
mapping = get_mapping(db, graph_name="FinReflectKG")
# exact key works:
translate("MATCH (n) RETURN labels(n)[0], count(n)", mapping=mapping)          # OK
# source Neo4j vocabulary fails:
translate("MATCH (a)-[:Has_Stake_In]->(b) RETURN b LIMIT 5", mapping=mapping)  # MAPPING_NOT_FOUND: Has_Stake_In
# real data type value fails too (analyzer renamed it):
translate("MATCH (m:FIN_METRIC) RETURN m LIMIT 5", mapping=mapping)            # MAPPING_NOT_FOUND: FIN_METRIC (have FINMETRIC)
PY
```

A full harness (transpile + execute the 22-query gold set, record results) lives in the reporting
project at `scripts/cypher_eval.py`; raw output in `data/cypher_eval_results.json`.

---

## Impact / why it belongs here

- Porting an existing Neo4j Cypher corpus is a primary use case; today it fails wholesale on
  vocabulary unless the caller rewrites every query to the analyzer's normalized spellings.
- The mapping is the transpiler's own contract; requiring callers to match its internal, lossy label
  spellings couples every downstream project to `arango-cypher-py` internals.
- Fixing this centrally (resolver normalization + alias + label fidelity) makes every consumer —
  including the schema-aware `nl2cypher` path — correct by construction.
