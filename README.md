# arango-cypher-py

A Python-native **Cypher → AQL transpiler** for [ArangoDB](https://arangodb.com/).

Translates [openCypher](https://opencypher.org/) queries into ArangoDB Query Language (AQL), supporting **property-graph (PG)**, **labeled-property-graph (LPG)**, and **hybrid** physical models.

## Features

- **Schema-aware translation** — resolves Cypher labels and relationship types against a conceptual→physical mapping so the generated AQL targets the correct collections and type fields.
- **Hybrid model support** — handles databases that mix PG (types-as-collections) and LPG (types-as-labels in generic collections) patterns, even within a single query path.
- **ANTLR-based Cypher parser** — parses Cypher into a parse tree using the openCypher ANTLR grammar.
- **Safe AQL output** — uses bind parameters (`@@collection`, `@param`) throughout; never interpolates user input.
- **Dot-path property access** — supports nested document property access (e.g. `n.address.zip`) in expressions and projections.
- **Arango Cypher profile** — JSON-serializable manifest (`get_cypher_profile`) and `validate_cypher_profile` for NL/agent gateways ([`docs/python_prd.md`](docs/python_prd.md) §2A).

## Status

> **Early development (v0.0.x)** — the transpiler handles core `MATCH` / `WHERE` / `RETURN` / `WITH` / `ORDER BY` / `LIMIT` patterns across PG, LPG, and hybrid mappings. See [Supported Cypher subset](#supported-cypher-subset) for details.

**Roadmap:** Broader Cypher coverage, compiler architecture (normalized AST / IR and logical plan), phased openCypher compliance, and **NL → Cypher → AQL** positioning (Arango Cypher profile, `arango.*` extensions) are described in [`docs/python_prd.md`](docs/python_prd.md) (§2A, §7A, §10A).

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
git clone https://github.com/ArthurKeen/arango-cypher-py.git
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
- `POST /connect` — authenticate to ArangoDB, returns session token
- `POST /disconnect` — tear down session
- `GET /connections` — list active sessions (admin/debug)
- `GET /connect/defaults` — `.env` defaults for connection dialog (never exposes password)
- `POST /translate` — Cypher → AQL + bind vars (no session needed)
- `POST /execute` — translate and execute (requires session token)
- `POST /validate` — syntax-only or parse+translate validation
- `POST /explain` — translate Cypher, run AQL EXPLAIN, return execution plan (requires session)
- `POST /aql-profile` — translate Cypher, execute with profiling, return runtime stats + results (requires session)
- `GET /cypher-profile` — JSON manifest for agents/NL gateways

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
arango_cypher/          # Cypher parser + translate API
  _antlr/              # ANTLR-generated lexer/parser/visitor
  parser.py            # Parse Cypher → parse tree
  translate_v0.py      # Parse tree → AQL translation engine
  profile.py           # Arango Cypher profile manifest (get_cypher_profile)
  api.py               # Public translate() / profile / validate APIs
  service.py           # FastAPI HTTP service + UI static mount

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
docs/                  # PRD, design docs, query corpus
```

## Running tests

```bash
# Unit + golden tests (no database needed)
pytest -m "not integration and not tck"

# Integration tests (requires ArangoDB — see docker-compose.yml, host port 28529)
docker compose up -d
RUN_INTEGRATION=1 pytest -m integration

# Profile integration tests only: isolated Arango on host port 28530 (auto start/stop)
RUN_INTEGRATION=1 pytest tests/integration/test_profile_integration.py -q

# All tests
pytest
```

`docker-compose.pytest.yml` publishes **28530→8529** so it does not clash with the dev stack on **28529** or a native Arango on **8529**. The `arango_pytest_url` session fixture starts and tears that stack down for `test_profile_integration.py`.

## Architecture

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

## Related projects

- [arango-cypher](https://github.com/ArthurKeen/arango-cypher) — Foxx/JS implementation (runs inside ArangoDB coordinators)
- [arangodb-schema-analyzer](https://github.com/ArthurKeen/arangodb-schema-analyzer) — schema detection and conceptual→physical mapping

## License

[MIT](LICENSE)
