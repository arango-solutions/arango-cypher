# Deploying arango-cypher-py as a BYOC service

Deploys the FastAPI service plus the Cypher Workbench onto an ArangoDB platform
cluster via the **Arango Container Manager** manual-packaging path: a single
flat tarball onto a platform-provided Python base image. No OCI registry, no
`docker build`.

**Verified live on `prod.demo.pilot.arango.ai`**, 2026-09-28: package
`arango-cypher-py 0.2.0-2`, service `arango-user-defined-ddrnc`, database `AIM`,
at `/_service/uds/_db/AIM/arango-cypher-py/` — 38 routes, Workbench serving with
both assets, 44 sample queries.

> `AIM` was a typo for `IAM` in the operator's `.env`; that database does not
> exist on the cluster (the platform mounted the instance there anyway).
> Deploys now target `IAM`. `release --replace` resolves the running instance
> by name, so it removes the `AIM`-mounted one before deploying into `IAM`.

This path is ported from `arango-ontoextract`, whose
`docs/container-manager-deployment.md` is the fuller reference for the platform
itself; everything below is what differs for this package.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Arango Container Manager pod (py12base + uv)                │
│                                                              │
│   ./entrypoint  (Python; line 1 is the literal `entrypoint`) │
│      ├─ uv pip install -e ".[service]"                       │
│      ├─ guard: loopback ARANGO_URL, missing analyzer         │
│      └─ exec uvicorn arango_cypher.service:app               │
│                                                              │
│   FastAPI app                                                │
│      ├─ /translate /execute /nl2cypher …  (38 routes)        │
│      ├─ /frontend  → ui/dist   (AMP mount)                   │
│      └─ /ui        → ui/dist   (legacy mount)                │
└───────────────────────┬──────────────────────────────────────┘
                        │ python-arango
                        ▼
              ArangoDB cluster (remote — never in this pod)
```

## Quick start

```bash
# SERVICE_ROOT_PATH must name the database ARANGO_DB (in .env) deploys into.
SERVICE_ROOT_PATH=/_service/uds/_db/IAM/arango-cypher-py \
  bash scripts/package-byoc.sh                     # build the tarball
uv run --group deploy arango-byoc-deploy list       # what already exists
uv run --group deploy arango-byoc-deploy release    # pre-flight, upload, swap, verify
uv run --group deploy arango-byoc-deploy verify     # probe the public URL
```

Deployment uses the shared [`arango-byoc-deploy`](https://github.com/ArthurKeen/arango-byoc-deploy)
tool, pinned in the `deploy` dependency group; this repo's settings live in
`[tool.arango-byoc]` in `pyproject.toml`. `release` replaces a running service
without asking (upload first, then delete and recreate): there is no
`--replace` flag any more.

Credentials come from repo-root `.env` (`ARANGO_URL`, `ARANGO_USER`,
`ARANGO_PASSWORD`, `ARANGO_DB`). Nothing is written to disk and no credential is
printed. `.env` is gitignored and is **not** bundled by default.

## What ships in the tarball

| Path in archive | Why |
| --- | --- |
| `entrypoint` | platform entry; **must** be at the root |
| `arango_cypher/` | the package |
| `pyproject.toml`, `uv.lock` | dependency resolution at boot |
| `ui/dist/` | the Workbench; `arango_cypher/service/ui.py` resolves `<root>/ui/dist` |
| `tests/fixtures/datasets/*/query-corpus.yml` | `/sample-queries` reads these (see below) |
| `.env` | **only** with `PACKAGE_INCLUDE_ENV=1` — off by default |

Flags: `PACKAGE_INCLUDE_UI=0` (headless API), `PACKAGE_BUILD_UI=0` (bundle the
existing `ui/dist` without rebuilding), `PACKAGE_INCLUDE_SAMPLES=0`,
`PACKAGE_USE_TOPDIR=1` (nested layout), `PACKAGE_INCLUDE_ENV=1`.

## Platform behaviours that will bite

**Entrypoint detection.** The platform runs `python /project/<first
whitespace-separated word of the file named entrypoint>`. Line 1 must therefore
begin with the literal token `entrypoint` — a shebang, docstring, comment or
import there makes it try to execute `python /project/"""` and fail with a bare
"No entrypoint found". Both the packager and `arango-byoc-deploy preflight` assert
this, because discovering it costs a full upload/deploy cycle.

**Flat archive.** `entrypoint` at the tar root, not nested under a directory.

**macOS xattrs.** Apple metadata (`com.apple.provenance`, `com.apple.quarantine`)
leaks into PAX headers and makes some Linux extractors fail with `stream closed:
EOF`. The packager exports `COPYFILE_DISABLE=1` and runs `xattr -cr`; both are
no-ops on Linux.

**No `pip` in the base venv.** `py12base` venvs frequently ship without a `pip`
module, so the entrypoint prefers `uv pip install`, falling back to `ensurepip`.
Pin the binary with `UV_BINARY` if `PATH` is minimal.

**Base images are per-cluster.** The house standard `py13base` **does not exist**
on `prod.demo.pilot.arango.ai`, which offers only `node22base`, `py12base`,
`py12cugraph`, `py12torch`, `test`. This package supports 3.11/3.12, so
`py12base` is the default here.

**All deploy env values must be strings.** The platform decodes the `env` map as
protobuf `string->string`; a JSON boolean is rejected with `invalid value for
string field value: true`. Hence `has_ui: "true"`.

**There is no update endpoint.** An update is delete-then-deploy, which is what
`release --replace` does. Without `--replace` the script refuses rather than
leaving two services on one instance name.

**`DEPLOYED` does not mean serving.** The platform reports `DEPLOYED` as soon as
the pod launches, but the entrypoint then installs dependencies — roughly 45
seconds before the first 200. `verify` is the real check; expect 404s before it.

**Platform login: what the pod gets, and how to check it.** Users are signed in
by the platform, not by the app. The gateway forwards each request with the
user's JWT (`Authorization: bearer`), and the operator injects:

| Variable | What it is |
| --- | --- |
| `ARANGO_DEPLOYMENT_ENDPOINT` | the in-cluster coordinator URL |
| `ARANGO_DEPLOYMENT_CA` | the CA that signs that endpoint's certificate; verify TLS against it |
| `INTEGRATION_HTTP_ADDRESS_FULL` / `INTEGRATION_HTTP_ADDRESS` | the integration sidecar: `GET /_integration/authn/v1/identity` names a token's user, `POST /_integration/authn/v1/createToken` mints one for a named user (for work that outlives a request; never mint without a user, the sidecar defaults to root) |

`GET <mount>/connect/platform/diagnostics` reports what the pod received and
whether each piece works, never a token. To open it in a browser, sign in to
the platform at `https://<host>/ui/` first, in the same browser; without that
the gateway answers `{"message":"Unauthorized"}` before the request reaches
the app. Checked on prod.demo IAM (0.2.0-14): CA injected and verifying,
sidecar naming the user, minted token accepted.

## Package-specific notes

**The `[service]` extra is mandatory.** `arango_cypher.service` calls
`_require_analyzer_unless_opted_out` at import, so a bundle without
`arangodb-schema-analyzer` cannot boot. The entrypoint installs `.[service]` and
fails with a named error if the analyzer is still missing. Override only with
`ARANGO_CYPHER_ALLOW_HEURISTIC=1`, accepting degraded mappings (PRD §7.1).

**`ROOT_PATH` is required, and its absence is near-invisible.** Bake it at
package time:

```bash
SERVICE_ROOT_PATH=/_service/uds/_db/<db>/<instance> bash scripts/package-byoc.sh
```

The platform's deploy `env` map carries platform metadata only — it does **not**
forward arbitrary application environment to the container, so passing
`ROOT_PATH` there is silently ignored (verified). The packager therefore writes
it into a minimal bundled `.env`, which `load_dotenv()` picks up at
`arango_cypher/service/app.py:30`.

Skip it and the Workbench still works — Vite's `base: "./"` makes its assets
prefix-relative — but `/docs` renders a Swagger page that requests
`/openapi.json` at the **cluster root** and so documents *ArangoDB's Core API*
instead of this service. A 200 on `/docs` is therefore not evidence that `/docs`
is correct; check which spec Swagger actually fetches.

**`/sample-queries` reads from `tests/`.** The handler
(`arango_cypher/service/routes/schema.py:588`) resolves
`<root>/tests/fixtures/datasets/*/query-corpus.yml`. Those files live under
`tests/` but are demo content, not test scaffolding — the first bring-up
returned `{"queries": []}` until the packager bundled them. 12K of YAML for 44
queries.

**No database lives in the pod.** The entrypoint refuses to start when
`ARANGO_URL`/`ARANGO_ENDPOINT` points at loopback, so the mistake is named at
boot rather than surfacing as a connection error on the first `/connect`.
Bypass for local repro with `ARANGO_CYPHER_ALLOW_LOOPBACK=1`.

## Environment variables

Set these in the Container Manager UI (preferred) or bundle a sanitized `.env`.

| Variable | Notes |
| --- | --- |
| `ARANGO_URL` / `ARANGO_ENDPOINT` | coordinator URL; both spellings accepted |
| `ARANGO_DB`, `ARANGO_USER`, `ARANGO_PASSWORD` | omit to make the Workbench connect interactively |
| `ARANGO_VERIFY_SSL` | `true` in production |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` | required for the NL → Cypher path only |
| `PORT`, `HOST`, `SERVICE_WORKERS` | uvicorn binding; the platform normally sets `PORT` |
| `ROOT_PATH` | only for absolute generated URLs — see above |
| `ARANGO_CYPHER_ALLOW_HEURISTIC` | start without the analyzer (degraded) |
| `ARANGO_CYPHER_ALLOW_LOOPBACK` | defeat the loopback guard (debug) |
| `ARANGO_CYPHER_SKIP_DEP_INSTALL` | skip the boot-time install for a pre-baked venv |
| `UV_BINARY` | pin `uv` when `PATH` is minimal |

## Rollback

Packages are immutable per `(name, version)` and every upload keeps its build
number, so rolling back is redeploying an earlier one:

```bash
uv run --group deploy arango-byoc-deploy list              # see the build numbers
uv run --group deploy arango-byoc-deploy rollback --to 0.2.0-1
```

`delete` removes the running service and leaves the uploaded packages alone.

## Open item

An `arango-transpiler` package (1.0.0, 1.0.1) is already uploaded to
`prod.demo` from an earlier containerization effort, with no service running
from it. This path deliberately uses the `arango-cypher-py` name instead; decide
whether to retire the old package or adopt its name before any wider rollout.
