# arango-cypher-py — deployment runbook

Operator-facing checklist for packaging and deploying this service to the Arango Platform Container Manager. Pairs with [`arango_packaging.md`](./arango_packaging.md) (upstream ServiceMaker docs) and with [PRD §15](../python_prd.md#15-packaging-and-deployment-to-the-arango-platform), which explains the design rationale.

## Scope

The default platform deployment is **headless**: library + CLI + FastAPI endpoints (`arango_cypher.service:app`). The Cypher Workbench UI (`ui/`) is not packaged and is not exposed by Container Manager — it is a developer / demo surface, not production (see PRD §4.4 scope note). All commands below target the headless tarball only.

## Prerequisites (read before packaging)

### 1. `arangodb-schema-analyzer` must be installable

**This is the packaging blocker today** (PRD §15.1). The `[analyzer]` optional-dependency extra pulls in `arangodb-schema-analyzer`, which is not on any package index. Inside the ServiceMaker build container (no network to private indexes, no git auth), `uv sync` with the `analyzer` extra will fail with

```
No matching distribution found for arangodb-schema-analyzer
```

There are two ways to unblock a deploy:

1. **Recommended (permanent fix)** — publish `arangodb-schema-analyzer` to PyPI (or the ArangoDB internal PyPI mirror). Tracked in [`~/code/arango-schema-mapper`](https://github.com/). Once published, pin it in `pyproject.toml` (`arangodb-schema-analyzer>=<published-version>`) and proceed.
2. **Interim (partial deploy)** — install *without* the `[analyzer]` extra. The analyzer powers the `/schema/introspect` route and the `arango-cypher-py mapping --strategy analyzer` CLI command (schema-from-live-DB generation). Every other code path — the Cypher→AQL transpiler, the NL→Cypher pipeline (WP-25), `/translate`, `/validate`, `/nl2cypher`, `/nl2aql` — works without it.

   Use this mode by *not* listing `analyzer` in the extras the build container installs. Operators who need the analyzer's precision can generate mappings offline (with an analyzer-installed dev checkout) and POST them via the existing mapping endpoints:

   ```bash
   # From a dev checkout with arangodb-schema-analyzer installed:
   python -m arango_cypher.cli mapping --strategy analyzer --db <my-db> > precise_mapping.json

   # Use this mapping in subsequent /translate calls or CLI operations.
   ```

Do not switch the dependency to a `git+ssh://` URL — rejected in PRD §15.2 because it bakes SSH auth into the build container.

### 2. Python and toolchain

- `requires-python = ">=3.10"` in `pyproject.toml`. ServiceMaker's Python 3.11 / 3.12 runtime base images are both fine.
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
RUN_PACKAGING=1 pytest tests/test_packaging_smoke.py -v
```

This test (gated off by default so day-to-day CI is fast) builds the tarball, unpacks it into a fresh `uv` venv, and runs `uv sync` against the `[service]` extra. If it passes, ServiceMaker should too. If the analyzer prerequisite above is not met and `analyzer` is included in the extras, it will fail fast here with the "no matching distribution" error — which is what you want.

## Versioning a snapshot

```bash
# After bumping pyproject.toml version and updating the changelog:
git tag -a v0.1.0-snapshot -m "snapshot for platform packaging"
git push origin v0.1.0-snapshot
```

