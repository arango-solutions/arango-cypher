# Contributing to arango-cypher-py

## Development setup

```bash
git clone https://github.com/arango-solutions/arango-cypher-py.git
cd arango-cypher-py

python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running tests

```bash
# Unit + golden tests (no database needed)
pytest -m "not integration and not tck"

# With coverage
pytest -m "not integration and not tck" --cov=arango_cypher --cov=arango_query_core

# Integration tests (requires ArangoDB)
docker compose up -d
RUN_INTEGRATION=1 pytest -m integration

# TCK harness (requires ArangoDB + explicit opt-in)
RUN_INTEGRATION=1 RUN_TCK=1 pytest -m tck
```

## Code style

This project uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting.

```bash
ruff check .
ruff format --check .
```

Configuration is in `pyproject.toml` under `[tool.ruff]`.

## Regenerating the ANTLR parser

If you modify `grammar/Cypher.g4`, regenerate the Python parser:

```bash
antlr4 -Dlanguage=Python3 -o arango_cypher/_antlr grammar/Cypher.g4
```

Do not hand-edit files in `arango_cypher/_antlr/`.

## Adding test cases

Translation test cases live in `tests/fixtures/cases/*.yml`. Each case has:

- `id` — unique identifier (e.g. `C070`)
- `name` — human-readable description
- `mapping_fixture` — which mapping to use (`pg`, `lpg`, `hybrid`, `movies_lpg`)
- `cypher` — the Cypher query
- `expected.aql` — the expected AQL output (or `null` if not yet implemented)
- `expected.bind_vars` — expected bind variables

Golden tests in `tests/test_translate_*_goldens.py` reference these cases by ID.

## Project structure

- `arango_cypher/` — Cypher parser and translator
- `arango_query_core/` — shared types (AQL, mappings, errors, extensions)
- `grammar/` — openCypher ANTLR grammar
- `tests/` — unit, golden, integration, and TCK tests
- `docs/` — PRD and design documents
