# GEMINI.md

## Project Overview
**arango-cypher-py** is a Python-native stack designed for translating Natural Language (NL) to Cypher and then to ArangoDB Query Language (AQL). It provides a robust transpiler that handles complex Cypher patterns and converts them into optimized AQL, supporting multiple physical data models (Property Graph, Labeled Property Graph, and Hybrid).

### Key Components
- **Transpiler:** ANTLR4-based parser and schema-aware emitter in `arango_cypher/`.
- **NL Pipeline:** WP-25 features including dynamic few-shot retrieval, fuzzy entity resolution, and execution-grounded validation in `arango_cypher/nl2cypher/`.
- **AQL Core:** Shared types, AQL building blocks, and mapping resolution in `arango_query_core/`.
- **HTTP Service:** FastAPI-based REST API in `arango_cypher/service.py` with endpoints for translation, execution, and NL processing.
- **Workbench UI:** A Vite/React/TypeScript web application for interactive Cypher-to-AQL development in `ui/`.
- **CLI:** A Typer-powered command-line interface in `arango_cypher/cli.py`.

## Building and Running

### Prerequisites
- Python 3.11+ (managed by `uv`; CI matrix 3.11 / 3.12)
- Node.js 18+ (for UI development)
- ArangoDB 3.11+

### Installation
The project uses `uv` for dependency management and builds.
```bash
# Install all dependencies (core, service, and dev)
uv sync
```

### Running the Service
```bash
# Start the FastAPI service via the entrypoint
python main.py

# Alternatively, via uvicorn directly
uvicorn arango_cypher.service:app --host 0.0.0.0 --port 8000 --reload
```

### Running the CLI
```bash
# General help
python -m arango_cypher.cli --help

# Translate Cypher to AQL
python -m arango_cypher.cli translate "MATCH (n:Person) RETURN n" -m mapping.json

# Execute Cypher against a live database
python -m arango_cypher.cli run "MATCH (n:Person) RETURN n" --db my_db
```

### UI Development
```bash
cd ui
npm install
npm run dev  # Vite dev server (proxies to backend at :8000)
```

### Building for Deployment
```bash
# Build the UI SPA
cd ui && npm run build && cd ..

# Package the Python project
uv build --sdist
```

## Testing
The project uses `pytest` with markers for different test tiers.
```bash
# Unit and Golden tests (fast, no DB needed)
pytest -m "not integration and not tck and not cross"

# Integration tests (requires ArangoDB at port 28529)
docker compose up -d
RUN_INTEGRATION=1 pytest -m integration

# openCypher TCK compliance tests
RUN_INTEGRATION=1 RUN_TCK=1 pytest -m tck

# NL2Cypher evaluation (requires LLM API keys)
RUN_NL2CYPHER_EVAL=1 pytest tests/test_nl2cypher_eval_gate.py
```

## Development Conventions

### Coding Standards
- **Linting & Formatting:** Managed by `ruff`.
  ```bash
  ruff check .
  ruff format .
  ```
- **Type Hints:** Strict type hinting is preferred throughout the codebase.
- **Safety:** Always use bind parameters in generated AQL; never interpolate user-supplied strings directly.

### Architecture & Patterns
- **Mapping Styles:**
  - **PG (Property Graph):** One collection per entity/relationship type.
  - **LPG (Labeled Property Graph):** Generic collections (`nodes`, `edges`) with type fields.
  - **Hybrid:** A mix of PG and LPG styles.
- **ANTLR Regeneration:**
  If the grammar at `grammar/Cypher.g4` is modified, regenerate the parser:
  ```bash
  antlr4 -Dlanguage=Python3 -visitor -o arango_cypher/_antlr grammar/Cypher.g4
  ```
- **Golden Tests:** New translation capabilities should be accompanied by fixture-based golden tests in `tests/fixtures/cases/`.

## Key Files
- `pyproject.toml`: Project configuration and dependencies.
- `main.py`: Entrypoint for Arango ServiceMaker and platform deployment.
- `arango_cypher/api.py`: Public API for translation and execution.
- `arango_cypher/service.py`: FastAPI application definition.
- `arango_cypher/nl2cypher/`: Core LLM/NL pipeline logic.
- `docs/python_prd.md`: Comprehensive Product Requirements Document.
- `docs/implementation_plan.md`: Current development roadmap and work packages.
