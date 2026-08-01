# AGENTS.md — arango-cypher-py

Canonical instructions for AI coding agents (Cursor, Claude, Gemini, etc.) working
in this repo. This file consolidates the former `CLAUDE.md` and `GEMINI.md`.

## Identity

- PROJECT_ID: arango-cypher-py
- PROJECT_TYPE: service
- PRD_FILE: docs/PRD.md
- TECH_STACK: Python (ANTLR4 Cypher→AQL transpiler + FastAPI service), Vite/React/TS UI, ArangoDB

## Project overview

**arango-cypher-py** is a Python-native **NL → Cypher → AQL** stack for ArangoDB.
Two paths share one engine:

- **Cypher → AQL transpiler** — deterministic translation of openCypher to AQL
  across property-graph (PG), labeled-property-graph (LPG), and hybrid physical
  models. ANTLR4 parser + schema-aware emitter in `arango_cypher/`.
- **NL → Cypher pipeline** — an LLM generates *conceptual* Cypher; the transpiler
  converts it to AQL. The LLM never sees physical mapping details; the transpiler
  never uses an LLM. (`arango_cypher/nl2cypher/`.)

Key components: AQL core (`arango_query_core/`), FastAPI HTTP service
(`arango_cypher/service.py`), Workbench UI (`ui/`, Vite/React/TS), CLI
(`arango_cypher/cli.py`).

## Source of truth

- **`docs/PRD.md`** is the single, consolidated PRD — the source of truth for what
  this system must do. All implementation must be traceable to it.
- Detailed records: `docs/python_prd.md` (full PRD + changelog),
  `docs/cypher_coverage_plan.md`, `docs/multitenant_prd.md`,
  `docs/implementation_plan.md`.
- If a requirement exists in code but not in `docs/PRD.md`, add it to the PRD.

## Build, run, test

Dependencies are managed with `uv` (CI matrix: Python 3.11 / 3.12; Node 18+ for
the UI; ArangoDB 3.11+).

```bash
uv sync                                            # install all deps
python main.py                                     # run the service (entrypoint)
uvicorn arango_cypher.service:app --reload         # ...or directly
python -m arango_cypher.cli --help                 # CLI
cd ui && npm install && npm run dev                # UI dev server (proxies :8000)
cd ui && npm run build                             # build the SPA (dist/ is gitignored)
```

Testing (markers in `pyproject.toml`):

```bash
pytest -m "not integration and not tck"            # fast unit + golden
docker compose up -d && RUN_INTEGRATION=1 pytest -m integration   # integration (Arango :28529)
docker compose -f docker-compose.neo4j.yml -p arango_cypher_neo4j up -d
RUN_INTEGRATION=1 RUN_CROSS=1 pytest tests/integration/test_movies_crossvalidate.py   # Neo4j cross-val
RUN_NL2CYPHER_EVAL=1 pytest tests/test_nl2cypher_eval_gate.py     # NL eval (needs LLM key)
```

> The dev shell may inject `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` that block DB
> traffic. Use `scripts/run_service.sh` (it strips them) or unset them first.

## Conventions

- **Safety:** generated AQL must use bind parameters; never interpolate
  user-supplied strings.
- **Lint/format:** `ruff check .` and `ruff format .`. Strict type hints preferred.
- **Mapping styles:** PG (one collection per type), LPG (generic collections +
  type field), hybrid (a mix). The mapping is authoritative — never hardcode.
- **ANTLR regen:** if `grammar/Cypher.g4` changes, regenerate with
  `antlr4 -Dlanguage=Python3 -o arango_cypher/_antlr grammar/Cypher.g4`.
- **Golden tests:** new translation capabilities ship with fixture-based golden
  tests in `tests/fixtures/cases/`.
- Additional enforced rules live in `.cursor/rules/` (read-before-write,
  test-what-you-touch, verify-before-done, incremental-over-atomic,
  comprehensiveness-over-simplification, modularity, checkpoint-regularly).

## Key files

| Path | Purpose |
| --- | --- |
| `docs/PRD.md` | Consolidated PRD (source of truth) |
| `main.py` | Service entrypoint (Arango ServiceMaker / platform deploy) |
| `arango_cypher/api.py` | Public `translate()` / profile / validate APIs |
| `arango_cypher/_translate_v0/` | Cypher→AQL transpiler core |
| `arango_cypher/service.py` | FastAPI app |
| `arango_cypher/nl2cypher/` | NL pipeline (few-shot, entity resolution, retry) |
| `arango_query_core/` | Shared AQL building blocks + `MappingResolver` |
| `grammar/Cypher.g4` | openCypher ANTLR grammar |

## Optional: dark-factory drift mode

If the local `.claude/` skills are present, this project supports autonomous PRD
drift detection:

- `/pattern-search <problem>` before solving a non-trivial problem.
- `/pattern-save` after fixing a drift gap or finding a reusable technique.
- `/prd-sync` at the end of any session that touched implementation files.

Drift policy: a MISSING requirement is a bug (not a TODO); a TEST-ONLY requirement
(tested but not implemented) must be fixed; never mark a requirement IMPLEMENTED
without a `file:line` reference. Shared memory lives in ArangoDB via the
`arangodb-mcp` server (`shared_patterns`, `project_registry`, `drift_alerts`).
