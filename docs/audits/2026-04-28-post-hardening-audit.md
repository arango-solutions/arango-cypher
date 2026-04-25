# Code-quality audit v2 — post-hardening sweep

**Audited by:** agentic pass on `main` at commit `dfa0427` (2026-04-28).
**Scope:** the whole-tree surface *after* the Wave 6a code-quality audit
closed (all ten TL;DR items landed across PRs #7, #8, #9, #10); plus the
follow-on work from Task 1 (PR #11) and Task 2 (PR #12). Subject areas:
security (identifier handling, rate limits, auth surface, logging leakage),
API surface hygiene, dead code / dead imports, test-coverage gaps, CI
config drift, module-size sprawl, config surface completeness, and
docs-to-code drift.
**Out of scope:** performance / scalability micro-benchmarks, LLM
provider quality, UI (\`ui/\`), tests themselves as a body of code.

---

## TL;DR

Nine findings. None are exploitable-today by an unauthenticated caller;
three are **moderate** (M) — authenticated elevation or silent
regression risk; six are **low** (L) — hygiene, coverage, or
defense-in-depth.

| # | Severity | Title | Action owner |
|---|---|---|---|
| 1 | **M** | `schema_acquire.compute_statistics` f-strings collection names into AQL (identifier interpolation without backticks or regex guard) | service |
| 2 | **M** | Rate limiter covers only 2 of ~35 endpoints — `/translate`, `/execute*`, `/explain`, `/aql-profile`, `/nl-samples`, `/schema/force-reacquire` are unlimited | service |
| 3 | **M** | CI `packaging` job path drift silently masked 0-test-collected runs from 2026-04-24 → 2026-04-28 (fixed in PR #12 amend, but category-level risk remains) | ops |
| 4 | L | `mapping_from_wire_dict` / `mapping_hash` (new public helpers from PR #10) have only indirect test coverage — no dedicated unit tests in `tests/test_arango_query_core_*.py` | tests |
| 5 | L | No `@field_validator` declarations on any `service.py` `BaseModel` request type — stricter-than-type validation (length, enum, URL shape) is absent | service |
| 6 | L | Structured logging is effectively absent in `service.py` — 4 log calls in 2016 LOC; no request-correlation ID, no per-endpoint timing log line | service |
| 7 | L | `ARANGO_PASS` / `ARANGO_PASSWORD` split between service and CLI is *documented* (`.env.example:9-15`) but still an operational footgun — both names should resolve to one | service + cli |
| 8 | L | `arango_cypher/translate_v0.py` at 5063 LOC and `arango_cypher/service.py` at 2016 LOC are splittable monoliths (not urgent — pre-existing, grew organically) | refactor |
| 9 | L | `ruff format --check` is intentionally skipped in CI (see the comment at `.github/workflows/ci.yml:19-21`) — follow-up tracked but never scheduled | ops |

Severity legend:

- **H** (High) — exploitable by an unauthenticated remote caller, or guaranteed data-integrity loss.
- **M** (Moderate) — authenticated-user elevation, silent-regression risk, or cross-cutting defense-in-depth hole.
- **L** (Low) — hygiene, observability, coverage, or paper-cut.

No **H**-severity findings.

---

## 1. `compute_statistics` f-strings collection names into AQL — **M**

> **Status: CLOSED — 2026-04-29 (audit-v2 batch 1).** Lifted `_COLLECTION_NAME_RE`
> into `arango_query_core.mapping` as `COLLECTION_NAME_RE` + `is_valid_collection_name()`
> and reworked all four AQL sites in `compute_statistics` to (a) regex-validate
> the collection name against the ArangoDB grammar before any DB call and
> (b) backtick-quote the identifier inside the AQL string. Invalid names
> short-circuit to `count=0` (same as the existing `except` branch).

**Where:** `arango_cypher/schema_acquire.py` lines 1344, 1356, 1375, 1388.

**What:**

```1344:1348:arango_cypher/schema_acquire.py
            try:
                cursor = db.aql.execute(f"RETURN LENGTH({col_name})")
                count = next(cursor, 0)
            except Exception:
                count = 0
```

```1356:1358:arango_cypher/schema_acquire.py
                aql = f"FOR d IN {col_name} FILTER d.`{type_field}` == @tv COLLECT WITH COUNT INTO c RETURN c"
                cursor = db.aql.execute(aql, bind_vars={"tv": type_value})
                entity_count = next(cursor, 0)
```

`col_name` and `edge_col` come from `emap.get("collectionName", label)` /
`rmap.get("edgeCollectionName", rtype)` — fields on a `MappingBundle`.
When the bundle is freshly analyzer-introspected, these names are
server-validated by ArangoDB before they ever enter the bundle, so the
interpolation is safe *in practice today*. But:

- Every adjacent `FOR d IN <coll> FILTER d.\`<field>\` == @tv` pattern in
  the same file uses bind-var style for the *field* (\`@tv\`) and
  backticks around the *identifier* (\`\`d.\`<field>\`\`\`). The
  collection name is the only piece left bare — an inconsistency that
  makes the call site look correct at a glance when it isn't.
- `compute_statistics` is a *public* function (no leading underscore).
  Any future caller that passes a user-constructed `MappingBundle`
  (e.g., a pipeline that edits mappings before stats refresh) becomes
  an authenticated-user AQL-injection surface — bounded by the
  session's DB privileges but trivially wider than the analyzer path.
- The service already has a blessed identifier guard —
  `_COLLECTION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,255}$")`
  at `service.py:438`, used by the `/tenants` endpoint at line 1649.
  Applying the same guard here would be one line per call site.

**Reproduces:** authenticated user POSTs a mapping with
`collectionName = "Users) REMOVE user IN Users RETURN LENGTH(Users"`,
then triggers a stats refresh. In a production deployment with
per-tenant DB privileges this still matters because the attacker-via-
stolen-session gets to the union of (their session privileges) ∪
(AQL-injection scope) which is strictly larger than (their session
privileges).

**Fix:**

- Wrap `col_name` / `edge_col` in backticks: `f"RETURN LENGTH(\`{col_name}\`)"`. Backticks alone prevent most forms because AQL grammar requires the identifier to balance them.
- Additionally, regex-validate against `_COLLECTION_NAME_RE` before interpolation and raise a typed error on mismatch. Lift the regex out of `service.py` into `arango_query_core` so it's reusable.
- Alternative (stronger but invasive): pre-resolve collection names to
  `@@coll` bind vars. AQL supports `FOR d IN @@coll`. Changes the shape
  of `compute_statistics` more significantly (every call becomes
  `bind_vars={"@coll": col_name}`).

**Estimate:** 1–2 h for the defensive form (backticks + regex guard),
half a day for the bind-var form with goldens.

---

## 2. Rate limiter covers only 2 of ~35 endpoints — **M**

> **Status: CLOSED — 2026-04-30 (audit-v2 batch 2).** Second-bucket
> half done. Added `_check_compute_rate_limit` (a separate
> `_TokenBucket` keyed off the new `COMPUTE_RATE_LIMIT_PER_MINUTE`
> env, default 100/min — an order of magnitude above the LLM bucket per
> the recommendation below) and wired it onto 14 endpoints:
> `/translate`, `/validate`, `/execute`, `/execute-aql`, `/explain`,
> `/aql-profile`, `/schema/introspect`, `/schema/summary`,
> `/schema/statistics`, `/schema/force-reacquire`, `/suggest-indexes`,
> `/mapping/export-owl`, `/mapping/import-owl`, `/tools/call`. Both
> buckets now share a `_client_key()` helper so the same client is
> tracked under the same identity in either bucket. Three new tests in
> `tests/test_service_middleware.py::TestComputeRateLimit` pin
> bucket-isolation, the auth/IP/anon key fallback, and the dep-wired
> 429 round-trip on `/validate`. Multi-worker shared-bucket redesign
> stays a separate Wave-6b WP per the audit text below.
>
> **Quick-fix half** (`/nl-samples` covered by `_check_nl_rate_limit`)
> shipped in audit-v2 batch 1, see PR #15 / commit `82d8d6a`.

**Where:** `arango_cypher/service.py` line 314 (`_check_nl_rate_limit`
definition), applied at lines 1458 and 1561 only.

**What:** The rate limiter is a single bucket per client keyed off
`NL_RATE_LIMIT_PER_MINUTE` (default 10). It is wired onto `/nl2cypher`
and `/nl2aql` but not onto:

| Endpoint | Cost profile | Current gate |
|---|---|---|
| `/translate` | CPU-heavy ANTLR parse + transpile, plus Pydantic validation over arbitrary Cypher | **none** |
| `/validate` | ANTLR parse-only | **none** |
| `/execute`, `/execute-aql`, `/explain`, `/aql-profile` | AQL round-trip against user's DB | session only |
| `/nl-samples` | **invokes an LLM** (same cost profile as `/nl2cypher`) | session in public mode only |
| `/schema/introspect`, `/schema/force-reacquire` | triggers full schema analyzer run | session only |
| `/schema/statistics` | per-collection AQL count queries, O(collections) | session only |
| `/suggest-indexes`, `/mapping/export-owl`, `/mapping/import-owl`, `/schema/summary`, `/tools/call` | pure compute, bounded | **none** |

The most concerning gap is **`/nl-samples`** because it invokes an LLM
with the same cost profile as `/nl2cypher` but without the
`_check_nl_rate_limit` dep. A caller (authenticated in public mode, or
anonymous otherwise) can burn token budget on this endpoint unboundedly.

`/translate` and `/execute` are the next tier: not LLM-gated, but still
CPU-bound and easy to wedge.

**Fix:**

- Add `Depends(_check_nl_rate_limit)` to `/nl-samples` immediately — its
  cost profile is identical to `/nl2cypher`.
- Consider a *second, cheaper* rate-limit bucket for
  `/translate`, `/validate`, `/execute*`, `/explain`, `/aql-profile`,
  `/schema/*` — e.g., `ARANGO_CYPHER_PER_MINUTE_DEFAULT` set an order
  of magnitude higher than the LLM bucket.
- The current limiter is in-memory per-process. For Wave 6b / multi-worker
  deployments it will need to be shared (Redis, Arango) or at least
  documented as "per-worker, deliberately lenient".

**Estimate:** 30 min for the `/nl-samples` quick fix. Half a day for a
second bucket with tests. Full redesign (multi-worker) is a separate
WP.

---

## 3. CI `packaging` job path drift masked 0-test runs — **M** (fixed, category-risk remains)

**Where:** `.github/workflows/ci.yml:57` (pre-PR #12 amend).

**What:** The `packaging` job ran
`pytest tests/test_packaging_smoke.py` — a path that never existed
in the repo. `pytest` exit code 5 ("no tests collected") is still a
non-zero exit and *would* have failed CI, except the job was added
before the test file was planned, so the failure mode was "job fails,
team accepts, job stays red and is later ignored". Verified by reading
the git history: the job was added alongside the runbook on 2026-04-24,
marked as a scaffold-ahead-of-test. PR #12 fixes the path but the
class-of-bug is still live:

**Category-level risk:** CI jobs reference file paths as strings with no
build-time guarantee that the path exists. A rename or move of any of
these paths will silently degrade CI coverage:

- `.github/workflows/ci.yml` — 3 `pytest` invocations, 0 path guards.
- `.github/workflows/nl2cypher-eval.yml` — runs `tests/nl2cypher/eval/runner.py`.

**Fix options:**

- **Cheapest:** change `pytest <path>` to `pytest <path> --co -q | grep "test session starts"` so the job fails when 0 tests are collected (stripped-down form of the pin-guard idea).
- **Better:** add a single `ci_paths.yaml` read by a `check-paths.yml` job that runs first and asserts each referenced path exists. One job per workflow change is traceable in PR diff.
- **Best:** move CI jobs to `pytest -m <marker>` wherever possible (already the pattern for `integration`) and ensure `RUN_PACKAGING=1` is caught by a marker, not a file path.

**Estimate:** 30 min for the cheapest fix, 2–3 h for the marker refactor.

---

## 4. `mapping_from_wire_dict` / `mapping_hash` — indirect coverage only — **L**

> **Status: CLOSED — 2026-04-29 (audit-v2 batch 1).** New
> `tests/test_mapping_helpers.py` with 18 dedicated cases covering
> snake/camel-case symmetry, snake-wins-when-both-present precedence,
> missing-keys → empty-dict normalisation, `MappingSource` pass-through,
> owl_turtle intentional-no-read, hash determinism across key insertion
> order, hash sensitivity to `{cs, pm}`, hash insensitivity to
> `metadata` and `owl_turtle` (the documented canonical contract),
> dict-vs-bundle-vs-stub equivalence, empty-input baseline, and the
> hasattr / unknown-type fallback branches.

**Where:** `arango_query_core/mapping.py`; introduced by PR #10.

**What:** The two new public helpers are exercised by
`tests/test_service_hardening.py::TestMappingHashKeyNormalisation` (three
cases, pinned against the per-module aliases in `corrections.py` /
`nl_corrections.py`) and by `tests/test_nl_corrections.py` — but there
is no `tests/test_arango_query_core_mapping.py` with dedicated unit
tests for the two helpers as public API. Edge cases not explicitly
covered:

- `mapping_from_wire_dict({})` — all three fields empty; does it return a `MappingBundle` with the expected defaults?
- Mixed snake_case + camelCase in one dict — last-one-wins, alphabetical, snake-preferred, camel-preferred? The code's `or` chain says "snake wins"; there's no test pinning that.
- `mapping_hash` on a `MappingBundle` vs. on a dict that serialises to the same bundle — are they equal? (They should be; no test asserts this.)
- `mapping_hash` collision resistance around `owl_turtle` — the helper ignores it, but nothing tests the documented ignore.
- `source=` parameter on `mapping_from_wire_dict` — exercised in service, but no standalone test.

**Fix:** add `tests/test_arango_query_core_mapping.py` with the five
cases above plus a round-trip property (`mapping_hash(mapping_from_wire_dict(d)) == mapping_hash(d)`).

**Estimate:** 1–2 h.

---

## 5. No `@field_validator` on request models — **L**

> **Status: CLOSED — 2026-05-01 (audit-v2 batch 3).** Every
> user-facing request model in `arango_cypher/service.py` now carries
> stricter-than-type bounds derived from a small set of module-level
> constants (`_MAX_CYPHER_LENGTH = 100_000`, `_MAX_AQL_LENGTH = 100_000`,
> `_MAX_NL_QUESTION_LENGTH = 4_000`, `_MAX_RETRY_HINT_LENGTH = 8_000`,
> `_MAX_TURTLE_LENGTH = 1_000_000`, `_MAX_NOTE_LENGTH = 4_000`,
> `_MAX_FIELD_LENGTH = 256`). Concretely:
> `TranslateRequest.cypher` / `ExecuteRequest.cypher` /
> `ValidateRequest.cypher` / `ExecuteAqlRequest.aql` /
> `CorrectionRequest.{cypher,original_aql,corrected_aql}` use the
> Cypher/AQL envelopes; `NL2CypherRequest.question` /
> `NL2AqlRequest.question` / `NLCorrectionRequest.question` use the
> NL envelope; `NL2CypherRequest.retry_context` uses the larger
> retry-hint envelope (parser/EXPLAIN error blobs); `OwlImportRequest.turtle`
> uses the 1 MB turtle cap; `ConnectRequest.{url,database,username,password}`,
> `TenantContextPayload.{property,value,display}`,
> `ToolCallRequest.name`, `*Request.database`, `*Request.session_token`,
> `CorrectionRequest.note` / `NLCorrectionRequest.note` use the
> small-field / note caps. `ConnectRequest.url` additionally carries a
> `@field_validator` that requires the value to start with `http://` or
> `https://` (defence in depth in front of the SSRF guard added in
> PR #7 — the validator catches the cheaper, more obviously-broken
> cases at request-validation time so the caller gets a clear 422
> instead of a deeper 4xx/5xx after the connect machinery has spun
> up). Side-fix to the `_sanitize_pydantic_errors` handler: stringify
> any `BaseException` instance left in `ctx[...]` before serialising,
> otherwise the raw `ValueError` from the URL validator crashed the
> 422 encoder. New `tests/test_request_model_validators.py` (12
> cases) pins the at-limit-passes / one-byte-over-rejects contract
> for `/translate`, `/execute-aql`, `/nl2cypher`, `/corrections`,
> and `/connect`.

**Where:** every `BaseModel` subclass in `arango_cypher/service.py`
(\`ConnectRequest\`, \`TranslateRequest\`, \`ExecuteRequest\`,
\`NL2CypherRequest\`, etc.).

**What:** \`rg @field_validator arango_cypher/service.py\` returns zero
matches. Type-level validation is present (via Pydantic's default
behaviour) but nothing stricter. Missing stricter-than-type constraints
that would be 1-line additions:

- `TranslateRequest.cypher` — no max-length. An attacker POSTing a 10 MB Cypher string ties up an ANTLR parser thread.
- `ConnectRequest.url` — no URL-shape validation; the SSRF guard catches the bad cases at *connect* time (PR #7) but rejecting at *validation* time is cheaper and gives a clearer error.
- `NL2CypherRequest.question` — no length bound. An attacker can push a novel-length question at an LLM.
- `CorrectionRequest.pattern` — no length bound; corrections are stored in SQLite and retrieved into every translation path.

**Fix:** add `@field_validator` with `max_length=` constraints on the
string-valued fields. Two-line change per field, plus one test asserting
a 422 on overlong input.

**Estimate:** half a day including tests.

---

## 6. Structured logging is effectively absent — **L**

**Where:** `arango_cypher/service.py` has **4** `logging.*` /
`logger.*` / `log.*` calls total in 2016 LOC. 3 of them are in
`_validation_error_handler` (422-error logging, added by PR #7).

**What:** FastAPI's default access log captures the HTTP verb + path +
status + timing. Beyond that, there is no:

- Request-correlation ID propagated through `session`, the Cypher →
  AQL translator, or the LLM provider client.
- Per-endpoint timing log line (some endpoints return `elapsed_ms` in
  the response body but nothing hits the log stream).
- Structured error logs (every error comes through as raw text on the
  HTTPException side).
- LLM call log with `(provider, model, prompt_tokens, completion_tokens, cost)` — this is tracked in the response body but not logged.

For a production deployment this is a gap: operators cannot correlate a
misbehaving session with a log trail; LLM cost tracking requires scraping
response bodies; a slow endpoint produces no breadcrumb on the server.

**Fix:** introduce a small `_logging.py` with a per-request
correlation-ID middleware and a `log_endpoint_timing(endpoint, elapsed_ms, **extras)` helper; wire it into all endpoints that already compute `elapsed_ms` (the compute is already there, just not logged). Three to five days of disciplined work to land cleanly.

**Note:** this is a **Wave 6b**-class WP, not a paper-cut. Calling it out
as **L** severity because nothing is *broken* today — the observability
hole just prevents certain kinds of production diagnostics.

---

## 7. `ARANGO_PASS` vs `ARANGO_PASSWORD` — **L**

> **Status: CLOSED — 2026-05-01 (audit-v2 batch 3).** New shared
> helper `arango_cypher/_env.py::read_arango_password(*, caller)`
> resolves the split. Contract: `ARANGO_PASSWORD` (canonical, matches
> the Postgres / Redis / MongoDB / MySQL / Cassandra `*_PASSWORD`
> convention) wins when set; `ARANGO_PASS` is accepted as a
> deprecated fallback and emits exactly one
> `DeprecationWarning` + `logging.WARNING` per (caller, fallback-name)
> pair so a long-running service that calls the helper per-request
> doesn't spam its log; neither set returns `""` (preserves the prior
> default); `ARANGO_PASSWORD=""` is treated as an intentional value
> (auth-less local dev) and does *not* fall through to the legacy
> name. Both call sites flipped: `arango_cypher/service.py` (the
> `/connect/defaults` body assembler) and `arango_cypher/cli.py`
> (the `_connect` factory). `.env.example` rewritten to flip the
> canonical / legacy labels and to document the deprecation timeline
> (legacy name removed at 1.0). New `tests/test_env_password.py`
> (8 cases) pins the contract: canonical wins, legacy warns once per
> caller, distinct callers each get their own warning, empty
> canonical short-circuits, and `_reset_warning_state_for_tests()`
> re-arms for tests that need to exercise the fallback path more
> than once.

**Where:** `arango_cypher/service.py:798` reads `ARANGO_PASS`;
`arango_cypher/cli.py:90` reads `ARANGO_PASSWORD`. Documented at
`.env.example:9-15`.

**What:** A developer who uses both the service and the CLI must set
both env vars to the same value. If they set only one, the other tool
silently uses the empty default. The 2026-04-25 changelog entry
acknowledges the split and the `.env.example` file calls it out — but
the split is still operationally surprising ("why doesn't my CLI
connect when the service just connected with the same env?").

**Fix:** pick a canonical name (`ARANGO_PASSWORD` matches most external
conventions) and make both tools read the new name first, falling back
to the legacy name with a deprecation log line. Ship a changelog entry
dating the legacy name's removal at the next major.

**Estimate:** 1–2 h.

---

## 8. `translate_v0.py` 5063 LOC, `service.py` 2016 LOC — **L**

**Where:** `arango_cypher/translate_v0.py`, `arango_cypher/service.py`.

**What:**

| File | LOC | Observation |
|---|---|---|
| `translate_v0.py` | 5063 | The Cypher-to-AQL transpiler. Grew organically from Wave 1. Natural split points: MATCH clause compilation, WHERE compilation, RETURN projection, SET/CREATE/MERGE mutation. |
| `service.py` | 2016 | FastAPI app + ~35 endpoints + Pydantic request/response models + session store + CORS startup guard + SSRF guard + rate limiter + validation-error handler. Natural split: `service/routes/*.py` by endpoint cluster, `service/security.py` (session, SSRF, rate limit, CORS, error redaction), `service/app.py` (app factory + startup). |

Neither is a correctness problem. But both are reviewer-hostile: a
2000-LOC file is ~40 screens of code. A 5000-LOC file is borderline
impossible to hold in working memory. The next substantive change to
either should split before extending.

**Fix:** split in a dedicated refactor PR; zero-behaviour-change, with
the file-rename diffs in a first commit and the symbol-move diffs in a
second so review is mechanical.

**Estimate:** 2–3 days for `service.py`, 4–5 days for `translate_v0.py`
(goldens per split).

---

## 9. `ruff format --check` intentionally skipped in CI — **L**

> **Status: CLOSED — 2026-04-30 (audit-v2 batch 2).** Format pass
> shipped as the first commit of this batch (`b0a3241`, 102 files
> reformatted, isolated from the substantive changes for clean
> diff-by-commit review). CI gate flipped on in
> `.github/workflows/ci.yml`: `ruff format --check .` now runs
> alongside `ruff check .` in the `lint` job. The deferred follow-up
> in the file's own comment is gone; the new comment cross-references
> the audit close-out and the format-pass commit.

**Where:** `.github/workflows/ci.yml:19-21`.

**What:** The comment says:

> `ruff format --check` intentionally omitted: ~95 files pre-date the
> format gate and rebreaking them in this PR would swamp reviewable
> changes. Track as follow-up: run `ruff format .` in an isolated PR.

That follow-up has not happened. Every PR since has had to `ruff format`
only the files it touches, and the 95-file backlog stays constant (or
grows, since new files aren't subject to a pre-commit format gate).

**Fix:** a single PR that runs `ruff format .`, contains zero other
changes, and lands during a quiet moment. Then flip the CI gate on.
Recommended sequence: land the flip *before* the next large feature
branch so the merge conflict noise happens on a PR the team owns rather
than a third party's.

**Estimate:** 1 h for the format pass + CI flip. Inconvenience cost
(conflict resolution on in-flight PRs) is the actual expense.

---

## What stayed the same vs. the Wave-6a audit

| Wave-6a concern | Status now |
|---|---|
| Credential leakage via 422 error responses | **Fixed** (PR #7) — `_sanitize_pydantic_errors` + `_redact_value`. Verified clean in this audit. |
| Credential leakage via `/connect/defaults` | **Fixed** (PR #7) — password empty by default, opt-in via `ARANGO_CYPHER_EXPOSE_DEFAULTS_PASSWORD`, disabled in public mode. |
| SSRF from `/connect` | **Fixed** (PR #7) — `_check_connect_target` covers cloud-metadata unconditionally + RFC1918 / loopback / link-local in public mode. |
| `*` + credentials CORS | **Fixed** (PR #7) — startup refuses this combination. |
| NL route auth in public mode | **Fixed** (PR #7) — `_require_session_in_public_mode` wired onto all NL routes. |
| Identifier injection at `/tenants?collection=` | **Fixed** (Wave 6a, 2026-04-24) — `_COLLECTION_NAME_RE` guard at service boundary. |
| Mapping-hash key-normalisation drift | **Fixed** (PR #10) — single `arango_query_core.mapping_hash` canonical. |
| `_mapping_from_dict` / `_dict_to_bundle` duplication | **Fixed** (PR #10) — both delegate to `mapping_from_wire_dict`. |
| `CypherVisitor` dead code | **Fixed** (PR #10) — deleted. |
| `arangodb-schema-analyzer` pin triplication | **Fixed** (PR #10) — self-referencing extras. |
| Stale post-0.6.1 narrative | **Fixed** (PR #9) — docs refresh. |
| `.env.example` completeness | **Fixed** (2026-04-25 housekeeping PR). Re-verified clean in this audit modulo the `ARANGO_PASS` split (finding #7 above). |

---

## Recommended next actions (priority order)

1. **Finding #2 quick-win** — add `_check_nl_rate_limit` to `/nl-samples` (30 min, one line + test).
2. **Finding #3 follow-up** — add a CI path-guard or marker refactor so that the class of bug that produced the PR #12 amend can't recur (30 min – 3 h depending on ambition).
3. **Finding #1 defensive fix** — backticks + `_COLLECTION_NAME_RE` guard on the four `compute_statistics` interpolation points; lift the regex into `arango_query_core` (1–2 h).
4. **Finding #4** — dedicated unit tests for the new `arango_query_core` helpers (1–2 h).
5. **Finding #5** — `@field_validator` sweep over request models (half-day).
6. **Finding #7** — unify `ARANGO_PASS` / `ARANGO_PASSWORD` (1–2 h).
7. **Finding #9** — `ruff format .` repo-wide pass + CI flip (1 h + conflict cost).
8. **Finding #6** — structured logging WP (Wave 6b-class; 3–5 d).
9. **Finding #8** — monolith splits (Wave 6b-class; 2–5 d each).

Items 1–4 together are ~1 dev-day and close the moderate-severity
findings; batching them into a single PR is the lowest-overhead path.
Items 5–7 are half-days each. Items 8–9 are Wave 6b-sized and should be
scheduled, not just filed.
