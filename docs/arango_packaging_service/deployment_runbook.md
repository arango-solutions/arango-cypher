# arango-cypher-py — deployment runbook

Operator-facing checklist for packaging and deploying this service to the Arango Platform Container Manager. Pairs with [`arango_packaging.md`](./arango_packaging.md) (upstream ServiceMaker docs) and with [PRD §15](../python_prd.md#15-packaging-and-deployment-to-the-arango-platform), which explains the design rationale.

## Scope

The default platform deployment is **headless**: library + CLI + FastAPI endpoints (`arango_cypher.service:app`). The Cypher Workbench UI (`ui/`) is not packaged and is not exposed by Container Manager — it is a developer / demo surface, not production (see PRD §4.4 scope note). All commands below target the headless tarball only.

## Prerequisites (read before packaging)

### 1. `arangodb-schema-analyzer` resolves from PyPI

**This used to be the packaging blocker. It is no longer** (PRD §15.1). As of 2026-04-23 the analyzer is published to PyPI, and `pyproject.toml` pins the floor at `arangodb-schema-analyzer>=0.6.1,<0.7` across all three consumer extras (`[analyzer]`, `[service]`, `[dev]`). Inside the ServiceMaker build container, `uv sync --extra service` resolves the analyzer from the public index without any private-registry, git-auth, or vendored-wheel plumbing.

If a deployment environment does not have outbound access to `pypi.org`, point `uv` at an internal mirror via `UV_INDEX_URL` / `UV_EXTRA_INDEX_URL` the same way every other PyPI-hosted runtime dependency is satisfied.

**Historical escape hatch (still present, still supported for air-gapped / heuristic-only deploys):**

- Install *without* the `[analyzer]` extra. The analyzer powers `/schema/introspect` and the `arango-cypher-py mapping --strategy analyzer` CLI. Every other code path — the Cypher→AQL transpiler, the NL→Cypher pipeline (WP-25), `/translate`, `/validate`, `/nl2cypher`, `/nl2aql` — works without it and falls back to the heuristic mapping tier when needed.
- Operators who need the analyzer's precision without network access during build can generate mappings offline (with an analyzer-installed dev checkout) and POST them via the existing mapping endpoints:

  ```bash
  # From a dev checkout with arangodb-schema-analyzer installed:
  python -m arango_cypher.cli mapping --strategy analyzer --db <my-db> > precise_mapping.json

  # Use this mapping in subsequent /translate calls or CLI operations.
  ```

- Setting `ARANGO_CYPHER_ALLOW_HEURISTIC=1` lets the service boot and serve `/translate` even if the analyzer import fails at runtime (see `.env.example`). Without that flag a failed analyzer import is treated as a hard startup error — desirable for production, opt-in for air-gapped / CI smoke environments.

Do not switch the dependency to a `git+ssh://` URL — rejected in PRD §15.2 because it bakes SSH auth into the build container, and unnecessary now that the analyzer is on PyPI.

### 2. Python and toolchain

- `requires-python = ">=3.11"` in `pyproject.toml`. ServiceMaker's Python 3.11 / 3.12 runtime base images are both fine; 3.10 is no longer supported (dropped 2026-04 alongside a `match`/`PEP 604`-typing sweep).
- Build and dependency resolution use `uv` (platform default).

### 3. Required runtime environment

Populated on the Container Manager deployment spec, **not** baked into the tarball. At minimum:

| Variable | Required | Notes |
|----------|----------|-------|
| `ARANGO_URL` | yes | e.g. `http://arangodb:8529`. Set to whatever the deployed service should reach. |
| `ROOT_PATH` | yes (Platform) | The subpath where the service is mounted on the Arango Platform (e.g., `/_service/uds/_db/my_db/my_mount`). Required for `/docs` and `/frontend` to load assets correctly. |
| `ARANGO_DB` | default `_system` | Default DB for `/connect` form pre-fill only; every request supplies its own. |
| `ARANGO_USER`, `ARANGO_PASS` | yes if using the defaults path | Same — used to pre-fill the `/connect-defaults` endpoint; per-request creds override. |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` | at least one, only if NL endpoints are used | Drives the `/nl2cypher` and `/nl2aql` pipelines. Pure Cypher→AQL does **not** need an LLM. |
| `LLM_PROVIDER` | optional | `openai` \| `anthropic` \| `openrouter`. Defaults to auto-detect in `get_llm_provider()`. |
| `CORS_ALLOWED_ORIGINS` | optional | Comma-separated or `*`. Tighten for production. |
| `SESSION_TTL_SECONDS`, `MAX_SESSIONS`, `NL_RATE_LIMIT_PER_MINUTE` | optional | In-memory session and rate-limit tuning. Defaults: 1800 s / 100 sessions / 10 rpm. |

A complete template lives in [`.env.example`](../../.env.example).

## Entry point and probes

- **Import path** for the ASGI app: `arango_cypher.service:app`.
- **Liveness / readiness**: `GET /health` — returns `200 {"status":"ok","service":"arango-cypher-py","version":"0.1.0"}`. No DB round-trip, no auth. Point Container Manager health checks here.
- **Port**: read `PORT` from env if the platform sets it; default `8000`.

## Packaging

Follows the manual path in [`arango_packaging.md`](./arango_packaging.md):

```bash
# From the repo root, with a clean working tree:
git clean -fdx                # optional — removes ui/dist, __pycache__, .venv, etc.
tar --exclude='.git' \
    --exclude='ui/node_modules' \
    --exclude='.venv' \
    --exclude='tests/integration/_*' \
    -czf arango-cypher-py-$(python -c 'import tomllib,pathlib;print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])').tar.gz \
    .
```

Produces `arango-cypher-py-0.1.0.tar.gz`. Upload and deploy per the upstream ServiceMaker / Container Manager docs.

## Smoke test (recommended before every snapshot handoff)

A minimal clean-container install, matching what ServiceMaker will do:

```bash
RUN_PACKAGING=1 pytest tests/integration/test_packaging_smoke.py -v
```

Lives at [`tests/integration/test_packaging_smoke.py`](../../tests/integration/test_packaging_smoke.py); gated off by default (`RUN_PACKAGING=1`) so day-to-day CI is fast. The test contains two cases:

- **`test_pyproject_extras_pin_published_versions_only`** — unconditional (runs on every `pytest` invocation, not gated). Parses `pyproject.toml` and refuses any `file:`, `./`, git / hg / svn, or ` @ `-direct-reference dependency in the `[analyzer]` / `[service]` / `[cli]` / `[owl]` / `[dev]` extras. Guards WP-19 acceptance criterion #3 as a standing regression. ~50 ms.
- **`test_sdist_builds_and_imports_with_service_extras`** — `RUN_PACKAGING=1`-gated end-to-end. Runs `python -m build --sdist` (produces `arango_cypher_py-<ver>.tar.gz`), creates a throwaway venv via `python -m venv`, installs the sdist with the `[service,analyzer]` extras via `pip install '<sdist>[service,analyzer]'`, and asserts `import arango_cypher.service` succeeds inside that venv. Uses the portable stdlib toolchain (`build` + `venv` + `pip`) rather than `uv` so CI doesn't need a non-stdlib install; if you prefer `uv` locally, `uv build --sdist && uv sync --extra service --extra analyzer` is equivalent. Typical runtime 25–90 s depending on pip cache and PyPI latency. If the analyzer prerequisite above is not met and `analyzer` is included in the extras, it fails fast here with the "no matching distribution" error — which is what you want.

## Versioning a snapshot

```bash
# After bumping pyproject.toml version and updating the changelog:
git tag -a v0.1.0-snapshot -m "snapshot for platform packaging"
git push origin v0.1.0-snapshot
```

