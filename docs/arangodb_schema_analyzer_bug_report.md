# Bug report: `arangodb-schema-analyzer` tool contract v1 response validation failures

This repo (`arango-cypher-py`) uses `arangodb-schema-analyzer` (from `~/code/arango-schema-mapper`) to generate mapping fixtures for transpiler tests.

While integrating via `schema_analyzer.tool.run_tool()` (tool contract v1), I hit **deterministic INTERNAL_ERROR failures** caused by **the tool validating its own responses against `response.schema.json`**.

## Summary of issues

### 1) `requestId` emitted as `null` breaks response schema
When a request omits `requestId`, `run_tool()` still emits `"requestId": null` in responses.

But `response.schema.json` defines:
- `requestId`: `{ "type": "string" }` (optional, but **not nullable**)

Result: the tool raises an internal validation error like:
- `Internal response validation failed: ["None is not of type 'string'"]`

**Fix**: only include `requestId` in the response object if it is a non-empty string.

### 2) Metadata keys were snake_case instead of contract camelCase
The response schema requires:
- `metadata.analyzedCollectionCounts`
- `metadata.detectedPatterns`

But the tool emitted:
- `metadata.analyzed_collection_counts`
- `metadata.detected_patterns`

Result: the tool raised internal validation errors like:
- `... is not valid under any of the given schemas`

**Fix**: emit metadata with `by_alias=True` and define field aliases for the contract keys.

## Reproduction (local)

Prereqs:
- ArangoDB reachable (e.g. `docker compose up -d` in `arango-cypher-py` and `.env` set)
- `arangodb-schema-analyzer` installed editable from `~/code/arango-schema-mapper`

Minimal repro:

```python
from schema_analyzer.tool import run_tool

resp = run_tool({
  "contractVersion": "1",
  "operation": "analyze",
  "connection": {
    "url": "http://localhost:28529",
    "database": "cypher_pg_fixture",
    "username": "root",
    "password": "openSesame"
  },
  "analysisOptions": {"timeoutMs": 60000, "sampleLimitPerCollection": 2, "useCache": False},
  "outputOptions": {"pretty": False, "includeSnapshot": False}
})
print(resp)
```

Expected: `ok: true` response with a valid `result.analysis`.

Observed (before fixes): `ok: false` with `INTERNAL_ERROR` due to self-validation failure.

## Patch applied locally (for reference)
I applied two minimal changes in `arangodb-schema-analyzer`:
- `schema_analyzer/tool.py`: omit `requestId` if not provided
- `schema_analyzer/types.py` + `schema_analyzer/tool.py`: add Pydantic aliases and use `model_dump(by_alias=True)` for metadata

If you want, I can open a PR in the `arango-schema-mapper` repo with these changes, or you can copy them from local modifications.

