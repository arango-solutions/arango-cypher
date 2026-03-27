# arango-cypher-py

A Python-native **Cypher → AQL transpiler** for [ArangoDB](https://arangodb.com/).

Translates [openCypher](https://opencypher.org/) queries into ArangoDB Query Language (AQL), supporting **property-graph (PG)**, **labeled-property-graph (LPG)**, and **hybrid** physical models.

## Features

- **Schema-aware translation** — resolves Cypher labels and relationship types against a conceptual→physical mapping so the generated AQL targets the correct collections and type fields.
- **Hybrid model support** — handles databases that mix PG (types-as-collections) and LPG (types-as-labels in generic collections) patterns, even within a single query path.
- **ANTLR-based Cypher parser** — parses Cypher into an AST using the openCypher ANTLR grammar.
- **Safe AQL output** — uses bind parameters (`@@collection`, `@param`) throughout; never interpolates user input.
- **Extension framework** — namespaced `arango.*` functions and procedures for ArangoDB-specific capabilities (full-text search, vector search, geo) behind an explicit policy flag.
- **Nested document / virtual edge support** — mapping-driven access to embedded objects and foreign-key references as conceptual relationships.

## Status

> **Early development (v0.0.x)** — the transpiler handles core `MATCH` / `WHERE` / `RETURN` / `WITH` / `ORDER BY` / `LIMIT` patterns and is expanding toward broader openCypher coverage.

## Quick start

### Requirements

- Python 3.10+
- An ArangoDB instance (local or remote) for integration tests

### Install

```bash
# Clone the repo
git clone https://github.com/<your-org>/arango-cypher-py.git
cd arango-cypher-py

# Create a virtualenv and install in editable mode
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Usage

```python
from arango_cypher import translate
from arango_query_core import MappingBundle

# Build or load a mapping bundle describing your schema
mapping = MappingBundle(...)

result = translate("MATCH (n:Person) RETURN n.name", mapping=mapping)
print(result.aql)        # generated AQL
print(result.bind_vars)  # bind parameters
```

## Project layout

```
arango_cypher/          # Cypher parser + translate API
  _antlr/              # ANTLR-generated lexer/parser/visitor
  parser.py            # Parse Cypher → AST
  translate_v0.py      # AST → AQL translation engine
  api.py               # Public translate() entry point

arango_query_core/      # Shared AQL building blocks
  mapping.py           # MappingBundle / MappingResolver
  aql.py               # AqlQuery / AqlFragment types
  errors.py            # CoreError hierarchy
  extensions.py        # Extension registry + policy

grammar/               # openCypher ANTLR grammar (Cypher.g4)
tests/                 # Unit, golden, and integration tests
docs/                  # PRD, design docs, query corpus
```

## Running tests

```bash
# Unit + golden tests (no database needed)
pytest -m "not integration and not tck"

# Integration tests (requires ArangoDB — see docker-compose.yml)
docker compose up -d
pytest -m integration

# All tests
pytest
```

## Architecture

The transpiler follows a layered pipeline:

1. **Parse** — Cypher source → ANTLR parse tree
2. **Normalize** — parse tree → internal AST (pydantic/dataclass models)
3. **Resolve** — labels, relationship types, and properties against the conceptual→physical mapping
4. **Emit** — AST + resolved mapping → AQL string + bind variables

The mapping layer supports three physical model styles:

| Style | Entities | Relationships |
|-------|----------|---------------|
| **PG** | One collection per type | One edge collection per relationship type |
| **LPG** | Generic collection + type field | Generic edge collection + type field |
| **Hybrid** | Mix of the above, per type | Mix of the above, per relationship type |

## Related projects

- [arango-cypher](https://github.com/<your-org>/arango-cypher) — Foxx/JS implementation (runs inside ArangoDB coordinators)
- [arangodb-schema-analyzer](https://github.com/<your-org>/arangodb-schema-analyzer) — schema detection and conceptual→physical mapping

## License

[MIT](LICENSE)
