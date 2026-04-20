# arango-cypher-py

A Python-native **NL → Cypher → AQL** stack for [ArangoDB](https://arangodb.com/).

Two paths, one codebase:

1. **Cypher → AQL transpiler** — translates [openCypher](https://opencypher.org/) queries into ArangoDB Query Language (AQL) across **property-graph (PG)**, **labeled-property-graph (LPG)**, and **hybrid** physical models.
2. **NL → Cypher pipeline** — an LLM generates *conceptual* Cypher from a natural-language question; the transpiler converts it to AQL. The LLM never sees physical mapping details.

## Features

### Cypher → AQL

- **Schema-aware translation** — resolves Cypher labels and relationship types against a conceptual→physical mapping so the generated AQL targets the correct collections and type fields.
- **Hybrid model support** — handles databases that mix PG (types-as-collections) and LPG (types-as-labels in generic collections) patterns, even within a single query path.
- **ANTLR-based Cypher parser** — parses Cypher into a parse tree using the openCypher ANTLR grammar.
- **Safe AQL output** — uses bind parameters (`@@collection`, `@param`) throughout; never interpolates user input.
- **Dot-path property access** — supports nested document property access (e.g. `n.address.zip`) in expressions and projections.
- **Arango Cypher profile** — JSON-serializable manifest (`get_cypher_profile`) and `validate_cypher_profile` for NL/agent gateways ([`docs/python_prd.md`](docs/python_prd.md) §2A).

### NL → Cypher pipeline (WP-25)

- **Multi-provider LLM support** — `OpenAIProvider`, `AnthropicProvider`, `OpenRouterProvider`. Auto-detection on API key presence or explicit `LLM_PROVIDER=openai|anthropic|openrouter`.
- **Dynamic few-shot retrieval (WP-25.1)** — `FewShotIndex` with BM25 ranking over shipped `movies/northwind/social` corpora; teaches the LLM the canonical patterns for the current schema.
- **Pre-flight entity resolution with fuzzy matching (WP-25.2)** — `EntityResolver` rewrites user-supplied string literals (`"Forest Gump"` → `"Forrest Gump"`) against the live DB before generation. Combines exact, contains, reverse-contains, and `LEVENSHTEIN_DISTANCE`-based scoring with a configurable `fuzzy_threshold`.
- **Execution-grounded validation (WP-25.3)** — translated AQL is run through `_api/explain` in the self-healing retry loop; semantic errors (missing collections, unbound variables) are fed back into the next prompt.
- **Prompt caching (WP-25.4)** — cache-friendly section ordering in `PromptBuilder`. OpenAI's automatic prefix caching (≥ 1024 tokens) just works; Anthropic gets an explicit `cache_control: {type: "ephemeral"}` split via `split_system_for_anthropic_cache()`. `cached_tokens` is propagated uniformly across providers and surfaced on `NL2CypherResult` / `NL2AqlResult` and the HTTP responses.
- **Evaluation harness + regression gate (WP-25.5)** — 31-case corpus across `movies_pg` + `northwind_pg`, five categories (baseline / few_shot_bait / typo / hallucination_bait / multi_hop), reproducible runner with `--with-db`, 5 pp / +20 % / +0.3-retry tolerance policy, and a nightly CI matrix across OpenAI + Anthropic.

**Quality baseline** (live, 31 cases, both fixture DBs seeded, all WP-25.1/.2/.3 paths engaged):

| Metric | OpenAI `gpt-4o-mini` | Anthropic `claude-haiku-4-5` |
|---|---|---|
| parse_ok | 100.0 % | 100.0 % |
| pattern_match | 93.5 % | **100.0 %** |
| baseline | 100 % | 100 % |
| few_shot_bait | 100 % | 100 % |
| hallucination_bait | 100 % | 100 % |
| multi_hop | 100 % | 100 % |
| typo | 66.7 % | **100 %** |
| retries_mean | 0.000 | 0.000 |

Baselines are committed at [`tests/nl2cypher/eval/baseline.json`](tests/nl2cypher/eval/baseline.json) and [`tests/nl2cypher/eval/baseline.anthropic.json`](tests/nl2cypher/eval/baseline.anthropic.json). Regenerate via `python -m tests.nl2cypher.eval.runner --config full --with-db --baseline`. See [`arango_cypher/nl2cypher/README.md`](arango_cypher/nl2cypher/README.md) for module internals.

## Status

> **Early development (v0.0.x).**
>
> - **Cypher → AQL transpiler** — handles core `MATCH` / `WHERE` / `RETURN` / `WITH` / `ORDER BY` / `LIMIT` patterns across PG, LPG, and hybrid mappings. See [Supported Cypher subset](#supported-cypher-subset) for details.
> - **NL → Cypher pipeline** — WP-25 closed 2026-04-20. All five sub-packages (dynamic few-shot, pre-flight entity resolution with fuzzy matching, execution-grounded validation, prompt caching across OpenAI/Anthropic, and the eval harness + regression gate) shipped on `main`. Live nightly CI matrix gates both providers against committed baselines.

**Roadmap:** Broader Cypher coverage, compiler architecture (normalized AST / IR and logical plan), phased openCypher compliance, and **NL → Cypher → AQL** positioning (Arango Cypher profile, `arango.*` extensions) are described in [`docs/python_prd.md`](docs/python_prd.md) (§2A, §7A, §10A). Post-WP-25 follow-ups tracked in the same doc.

## Supported Cypher subset

The v0 translator supports:

- `MATCH` with single or multiple pattern parts, multiple `MATCH` clauses, multi-hop relationships
- `WHERE` with boolean logic (`AND`, `OR`, `NOT`, `XOR`), comparisons, `IN`, `IS NULL` / `IS NOT NULL`
- `RETURN` with projections, aliases, `DISTINCT`, `ORDER BY`, `SKIP`, `LIMIT`
- `WITH` for aggregation (`count`, `avg`, `sum`, `min`, `max`, `collect`) and pipeline stages
- `WITH ... MATCH` (tail match after aggregation/filtering)
- Functions: `size`, `toLower`, `toUpper`, `coalesce`, `type(r)`
- Inline pattern properties: `(n:User {id: "u1"})`, `-[:ACTED_IN {role: "Forrest"}]->`
- Named parameters: `$paramName`
- Arithmetic and unary `+`/`-` in expressions (e.g. in `WHERE` and `RETURN`)
- Bounded variable-length relationship patterns (e.g. `*1..2`, `*` with defaults)
- `UNION` / `UNION ALL` between compatible `RETURN` shapes (lowered to AQL `UNION_DISTINCT()` / `UNION()`)
- `OPTIONAL MATCH` — both as a companion to `MATCH` (LET/FIRST subquery) and as the sole reading clause (null-fallback wrapper)

- `arango.*` extension functions via registry: search (`bm25`, `tfidf`, `analyzer`), vector (`cosine_similarity`, `l2_distance`, `approx_near_cosine`, `approx_near_l2`), geo (`distance`, `geo_distance`, `geo_contains`, `geo_intersects`, `geo_in_range`, `geo_point`), document (`attributes`, `has`, `merge`, `unset`, `keep`, `zip`, `value`, `values`, `flatten`, `parse_identifier`, `document`)

- String predicates: `STARTS WITH`, `ENDS WITH`, `CONTAINS`

- `UNWIND` — standalone, before MATCH, or after MATCH (lowered to AQL `FOR ... IN`)

- `CASE` expressions — generic (`CASE WHEN ... THEN ... END`) and simple form (auto-expanded)

- `CALL ... YIELD` — standalone and in-query procedure calls; `CALL arango.*` procedures via registry (`fulltext`, `near`, `within`, `shortest_path`, `k_shortest_paths`)

- Embedded relationships — mapping-driven `EMBEDDED` style lowers `(u:User)-[:HAS_ADDRESS]->(a:Address)` to `LET a = u.address` (object) or `FOR t IN TO_ARRAY(u.tags)` (array), no edge collection needed

**Not yet supported:** multiple relationship types in one hop (`[:A|B]`), list/map comprehensions, write clauses (`CREATE`/`MERGE`/`SET`/`DELETE`).

## Quick start

### Requirements

- Python 3.10+
- An ArangoDB instance (local or remote) for integration tests

### Install

```bash
git clone https://github.com/arango-solutions/arango-cypher-py.git
cd arango-cypher-py

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Usage

```python
from arango_cypher import translate
from arango_query_core import MappingBundle

mapping = MappingBundle(
    conceptual_schema={"entityTypes": ["Person"], "relationshipTypes": ["KNOWS"]},
    physical_mapping={
        "entities": {
            "Person": {"style": "COLLECTION", "collectionName": "persons"}
        },
        "relationships": {
            "KNOWS": {"style": "DEDICATED_COLLECTION", "edgeCollectionName": "knows"}
        },
    },
)

result = translate("MATCH (n:Person) RETURN n.name", mapping=mapping)
print(result.aql)        # generated AQL
print(result.bind_vars)  # bind parameters
```

### NL → Cypher → AQL

```python
from arango import ArangoClient
from arango_cypher.nl2cypher import nl_to_cypher, get_llm_provider

db = ArangoClient(hosts="http://localhost:28529").db(
    "movies_pg", username="root", password="openSesame",
)

result = nl_to_cypher(
    "who acted in 'Forest Gump'?",      # typo is intentional
    mapping=mapping,
    llm_provider=get_llm_provider(),    # auto-picks OpenAI / Anthropic / OpenRouter from env
    use_fewshot=True,                   # WP-25.1 — BM25 dynamic few-shot
    use_entity_resolution=True,         # WP-25.2 — pre-flight fuzzy resolution
    db=db,                              # enables WP-25.2 (needs live DB) and WP-25.3 EXPLAIN-grounded retry
)

print(result.cypher)         # MATCH (p:Person)-[:ACTED_IN]->(m:Movie {title: "Forrest Gump"}) ...
print(result.aql)            # FOR p IN persons ...
print(result.cached_tokens)  # provider-agnostic cache-hit count
```

Configuration is environment-driven — set `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `OPENROUTER_API_KEY` (or pin explicitly with `LLM_PROVIDER=openai|anthropic|openrouter`). Without any key, the pipeline degrades to a rule-based fallback. See [`arango_cypher/nl2cypher/README.md`](arango_cypher/nl2cypher/README.md) for the degradation matrix and cache-telemetry details.

### Schema change detection

For long-running services that repeatedly call `get_mapping()`, the library exposes a cheap read-only probe so callers can skip unnecessary re-introspection:

```python
from arango_cypher import describe_schema_change, get_mapping, invalidate_cache

report = describe_schema_change(db)
# report.status: "unchanged" | "stats_changed" | "shape_changed" | "no_cache"
# report.unchanged, report.needs_full_rebuild — ergonomic predicates

if report.unchanged:
    # Skip prompt rebuilds, downstream cache busting, client re-notification.
    ...
else:
    # Let get_mapping() handle it. It picks the cheapest path internally:
    #   "stats_changed"  → reuse mapping, refresh cardinality stats (~50 ms)
    #   "shape_changed"  → full re-introspection
    mapping = get_mapping(db)
```

Two fingerprints drive the decisions:

- **Shape fingerprint** — hashes the collection set, types, and full index digests (type + fields + `unique` + `sparse` + VCI + `deduplicate`). Stable under ordinary writes; changes when the schema shape changes.
- **Full fingerprint** — shape + per-collection row counts. Triggers the stats-only refresh path when it differs but the shape fingerprint matches.

Caching is two-tier: a process-local `dict` (same-session hits) sits in front of an ArangoDB-collection cache (default: `arango_cypher_schema_cache`). The persistent cache survives service restarts and is shared across service instances pointed at the same DB — the containerized Arango Platform deployment path benefits directly.

Pass `cache_collection=None` to `get_mapping` / `describe_schema_change` when running as a read-only user who can't create collections; the in-memory cache still works. `force_refresh=True` rebypasses both tiers. `invalidate_cache(db)` wipes both tiers after a manual migration.

The same surface is exposed through the HTTP service so UI clients, platform orchestrators, and monitoring probes can act on change detection without embedding Python:

```bash
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/schema/status
# { "status": "unchanged", "unchanged": true, "needs_full_rebuild": false,
#   "current_shape_fingerprint": "…", "cached_shape_fingerprint": "…", … }

curl -X POST -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/schema/invalidate-cache?persistent=true"
# { "invalidated": true, "persistent": true }
```

`GET /schema/status` runs the same cheap probe (~20 ms for a 50-collection schema) and returns the same four status values. `POST /schema/invalidate-cache` drops both cache tiers by default; pass `?persistent=false` to drop only the process-local tier when you want the persistent cache to survive (e.g. after a replica-local administrative action that doesn't affect shared DB state).

### Arango Cypher profile (NL / agents)

```python
import json
from arango_cypher import get_cypher_profile, validate_cypher_profile

manifest = get_cypher_profile()
print(json.dumps(manifest["supported"], indent=2))

# Syntax-only (no mapping)
assert validate_cypher_profile("MATCH (n:Person) RETURN n").ok

# Parse + translate (same rules as translate()); reuse *mapping* from above
v = validate_cypher_profile(
    "MATCH (n:Person) RETURN n.name",
    mapping=mapping,
)
assert v.ok or v.first_error_code
```

Keep the manifest aligned with the shipped translator; bump `profile_schema_version` in `arango_cypher/profile.py` when the JSON shape changes.

### `arango.*` extension functions

```python
from arango_query_core import ExtensionPolicy, ExtensionRegistry
from arango_cypher import register_all_extensions, translate

registry = ExtensionRegistry(policy=ExtensionPolicy(enabled=True))
register_all_extensions(registry)  # search + vector + geo + document + procedures

result = translate(
    "MATCH (n:Person) RETURN arango.bm25(n) AS score",
    mapping=mapping, registry=registry,
)
print(result.aql)  # ... BM25(n) ...

# CALL arango.* procedures
result = translate(
    "CALL arango.fulltext('persons', 'name', 'Alice') YIELD doc RETURN doc",
    mapping=mapping, registry=registry,
)
print(result.aql)  # FOR doc IN FULLTEXT(...) RETURN doc
```

### HTTP service

```bash
# Install service dependencies
pip install -e ".[service]"

# Start the service
uvicorn arango_cypher.service:app --host 0.0.0.0 --port 8000
```

Endpoints:

**Connection & session**
- `POST /connect` — authenticate to ArangoDB, returns session token
- `POST /disconnect` — tear down session
- `GET /connections` — list active sessions (admin/debug)
- `GET /connect/defaults` — `.env` defaults for connection dialog (password included only in public/dev mode)

**Cypher → AQL**
- `POST /translate` — Cypher → AQL + bind vars (no session needed)
- `POST /execute` — translate and execute (requires session token)
- `POST /validate` — syntax-only or parse+translate validation
- `POST /explain` — translate Cypher, run AQL EXPLAIN, return execution plan (requires session)
- `POST /aql-profile` — translate Cypher, execute with profiling, return runtime stats + results (requires session)
- `GET /cypher-profile` — JSON manifest for agents/NL gateways

**NL → Cypher / NL → AQL**
- `POST /nl2cypher` — natural-language question → conceptual Cypher + translated AQL (optional DB-grounded entity resolution and EXPLAIN validation when a session is attached). Response includes `cached_tokens` for provider cost telemetry.
- `POST /nl2aql` — natural-language question → AQL directly (exposes physical mapping; deliberately separate from the Cypher path, see [`docs/python_prd.md`](docs/python_prd.md) §1.3).

### Cypher Workbench UI

A browser-based workbench with side-by-side Cypher and AQL editors, Explain/Profile support, and results display.

```bash
# Build the UI (requires Node.js 18+)
cd ui && npm install && npm run build && cd ..

# Start the service (serves both API and UI)
uvicorn arango_cypher.service:app --host 0.0.0.0 --port 8000

# Open http://localhost:8000/ui in your browser
```

**For development** (hot-reload):
```bash
# Terminal 1: FastAPI backend
uvicorn arango_cypher.service:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2: Vite dev server (proxies API calls to :8000)
cd ui && npm run dev
# Open http://localhost:5173
```

Features:
- **Cypher editor** (left) — syntax highlighting, bracket matching, auto-close, keyboard shortcuts
- **AQL editor** (right) — syntax-highlighted transpiled output, bind-vars panel
- **Toolbar** — Translate (Ctrl/Cmd+Enter), Run (Shift+Enter), Explain (Ctrl/Cmd+Shift+E), Profile (Ctrl/Cmd+Shift+P)
- **Results panel** — Table, JSON, Explain tree, Profile stats tabs
- **Connection dialog** — connect to any ArangoDB instance, pre-filled from `.env` defaults
- **Mapping editor** — JSON editor for the conceptual-to-physical mapping

## Project layout

```
arango_cypher/          # Cypher parser + translate API + NL pipeline
  _antlr/              # ANTLR-generated lexer/parser/visitor
  parser.py            # Parse Cypher → parse tree
  translate_v0.py      # Parse tree → AQL translation engine
  profile.py           # Arango Cypher profile manifest (get_cypher_profile)
  api.py               # Public translate() / profile / validate APIs
  service.py           # FastAPI HTTP service + UI static mount
  nl2cypher/           # WP-25: NL → Cypher → AQL pipeline
    _core.py           #   PromptBuilder, rule-based fallback, nl_to_cypher()
    _aql.py            #   Direct NL → AQL path (exposes physical mapping)
    providers.py       #   OpenAI / Anthropic / OpenRouter providers
    fewshot.py         #   FewShotIndex + BM25Retriever  (WP-25.1)
    entity_resolution.py #   EntityResolver w/ fuzzy matching (WP-25.2)
    corpora/*.yml      #   Seed few-shot corpora (movies / northwind / social)

arango_query_core/      # Shared AQL building blocks
  mapping.py           # MappingBundle / MappingResolver
  aql.py               # AqlQuery / AqlFragment types
  errors.py            # CoreError hierarchy
  extensions.py        # Extension registry + policy

ui/                    # Cypher Workbench UI (React + TypeScript + Vite)
  src/
    components/        # CypherEditor, AqlEditor, ResultsPanel, MappingPanel, ConnectionDialog
    lang/              # CodeMirror 6 language modes (Cypher, AQL)
    api/               # HTTP client + app state management
  dist/                # Built SPA (served by FastAPI at /ui)

grammar/               # openCypher ANTLR grammar (Cypher.g4)
tests/                 # Unit, golden, and integration tests
  nl2cypher/eval/      # WP-25.5 eval harness: corpus + configs + runner + baselines
docs/                  # PRD, design docs, query corpus
.github/workflows/     # CI (ci.yml) + nightly NL-eval matrix (nl2cypher-eval.yml)
```

## Running tests

```bash
# Unit + golden tests (no database, no LLM needed)
pytest -m "not integration and not tck"

# Integration tests (requires ArangoDB — see docker-compose.yml, host port 28529)
docker compose up -d
RUN_INTEGRATION=1 pytest -m integration

# Profile integration tests only: isolated Arango on host port 28530 (auto start/stop)
RUN_INTEGRATION=1 pytest tests/integration/test_profile_integration.py -q

# Cross-validate translated AQL against reference Neo4j (same query corpus,
# result-set diff). Bolt on host port 27687.
docker compose -f docker-compose.neo4j.yml -p arango_cypher_neo4j up -d
pip install 'arango-cypher-py[neo4j]'
RUN_INTEGRATION=1 RUN_CROSS=1 pytest tests/integration/test_movies_crossvalidate.py -q
docker compose -f docker-compose.neo4j.yml -p arango_cypher_neo4j down

# NL → Cypher evaluation harness (opt-in; requires a live LLM provider)
OPENAI_API_KEY=sk-...                                          \
    RUN_NL2CYPHER_EVAL=1 NL2CYPHER_EVAL_USE_DB=1               \
    ARANGO_URL=http://localhost:28529 ARANGO_USER=root         \
    ARANGO_PASS=openSesame                                     \
    pytest tests/test_nl2cypher_eval_gate.py -v

# Anthropic row (needs baseline.anthropic.json + claude-haiku-4-5 by default)
LLM_PROVIDER=anthropic ANTHROPIC_MODEL=claude-haiku-4-5        \
    NL2CYPHER_EVAL_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-... \
    RUN_NL2CYPHER_EVAL=1 NL2CYPHER_EVAL_USE_DB=1               \
    pytest tests/test_nl2cypher_eval_gate.py -v

# Refresh / generate eval reports (CLI)
python -m tests.nl2cypher.eval.runner --config full --with-db
python -m tests.nl2cypher.eval.runner --config full --with-db --baseline  # overwrite baseline.json

# All tests
pytest
```

### Continuous integration

- **`.github/workflows/ci.yml`** — ruff lint, unit tests on Python 3.10/3.11/3.12, integration tests against a CI-spun ArangoDB 3.11. Runs on every push / PR to `main`.
- **`.github/workflows/nl2cypher-eval.yml`** — nightly NL → Cypher regression gate. Spins up ArangoDB 3.11, seeds the eval fixtures, then runs the live gate as a `strategy.matrix` of two provider rows:
  - `openai` → `gpt-4o-mini` → gates against [`tests/nl2cypher/eval/baseline.json`](tests/nl2cypher/eval/baseline.json)
  - `anthropic` → `claude-haiku-4-5` → gates against [`tests/nl2cypher/eval/baseline.anthropic.json`](tests/nl2cypher/eval/baseline.anthropic.json)

  Required secrets: `OPENAI_API_KEY` (~$0.05/night), `ANTHROPIC_API_KEY` (~$0.10/night). Each row self-skips cleanly if its secret is absent. `fail-fast: false` so a single-provider regression doesn't mask the other. Cron `0 6 * * *` + `workflow_dispatch` for manual refreshes. This workflow does **not** block PRs (regression signal, not merge gate).

`docker-compose.pytest.yml` publishes **28530→8529** so it does not clash with the dev stack on **28529** or a native Arango on **8529**. The `arango_pytest_url` session fixture starts and tears that stack down for `test_profile_integration.py`.

`docker-compose.neo4j.yml` publishes Bolt on **27687** and the Browser on **27474**, so it coexists with a native Neo4j on 7687. The cross-validation harness (`tests/integration/test_movies_crossvalidate.py`) runs every Cypher query in `tests/fixtures/datasets/movies/query-corpus.yml` against Neo4j (the reference Cypher engine) and against the translated AQL, then diffs the two result sets row-by-row. Override connection settings with `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASS`.

## Architecture

### Cypher → AQL

The transpiler follows a layered pipeline:

1. **Parse** — Cypher source → ANTLR parse tree
2. **Resolve** — labels, relationship types, and properties against the conceptual→physical mapping
3. **Emit** — parse tree + resolved mapping → AQL string + bind variables

The mapping layer supports three physical model styles:

| Style | Entities | Relationships |
|-------|----------|---------------|
| **PG** | One collection per type | One edge collection per relationship type |
| **LPG** | Generic collection + type field | Generic edge collection + type field |
| **Hybrid** | Mix of the above, per type | Mix of the above, per relationship type |

### NL → Cypher → AQL

```
NL question
   │
   ├── FewShotIndex.search()          ← WP-25.1  BM25 over shipped corpora
   ├── EntityResolver.resolve()       ← WP-25.2  live-DB fuzzy (Levenshtein + contains)
   │
   ▼
PromptBuilder.render_system()        ← cache-friendly section ordering (WP-25.4)
   │   ┌─ prelude + schema          (static — cache target)
   │   └─ few-shot + resolved (dynamic)
   ▼
LLMProvider.generate()                ← OpenAI / Anthropic / OpenRouter
   │
   ▼
Cypher candidate
   │
   ├─ arango_cypher.parse()+translate()  ← reuse the Cypher→AQL stack
   ├─ explain_aql(db, aql)               ← WP-25.3  AQL EXPLAIN validation
   │
   ▼ (on failure)
retry loop with error-context injection
   │
   ▼
NL2CypherResult { cypher, aql, bind_vars, cached_tokens, retries }
```

The LLM only sees the **conceptual** schema — label names, relationship types, properties — never the physical mapping. That invariant (§1.2 of the PRD) is why the NL path and the transpiler path remain cleanly decoupled: the `nl_to_aql()` alternative in `arango_cypher/nl2cypher/_aql.py` is deliberately separate, takes the full physical mapping as input, and is only used where the extra latitude is worth the loss of the invariant.

## Related projects

- [arango-cypher-foxx](https://github.com/ArthurKeen/arango-cypher-foxx) — Foxx/JS implementation (runs inside ArangoDB coordinators)
- [arangodb-schema-analyzer](https://github.com/ArthurKeen/arangodb-schema-analyzer) — schema detection and conceptual→physical mapping

## License

[MIT](LICENSE)
