# Polyglot strategy: shared `arango-query-core` + language front-ends

This document proposes a shared Python core library, **`arango-query-core`**, that can be used by:
- `arango-cypher-py` (Cypher → AQL)
- `arango-sparql-py` (SPARQL → AQL)
- later: Gremlin/GQL front-ends

Goal: align early on **common mapping, AQL emission, execution, and Arango extensions**, while allowing each language to keep its own parser/AST/semantics.

---

## 1) Repository / packaging options

### Option A (recommended): 3 repos
- `arango-query-core` (shared library)
- `arango-cypher-py` (front-end)
- `arango-sparql-py` (front-end)

### Option B: monorepo with multiple packages
`packages/query_core`, `packages/cypher`, `packages/sparql`

Either way, the **API described below** is what front-ends code against.

---

## 2) `arango-query-core` module layout (proposed)
- `arango_query_core.mapping`
- `arango_query_core.aql`
- `arango_query_core.extensions`
- `arango_query_core.exec`
- `arango_query_core.errors`

Design principle: core is **deterministic** and **LLM-free**; agentic add-ons belong in separate optional packages.

---

## 3) Mapping bundle API surface

### 3.1 Inputs and data contracts
Core mapping should be grounded in the **stable transpiler export** from `arangodb-schema-analyzer` (a.k.a. `arango-schema-mapper`):
- library call or tool-contract call
- output contains:
  - `conceptualSchema`
  - `physicalMapping`
  - `metadata`

Core should treat that export as the canonical schema/mapping contract.

### 3.2 Types (Python)
Proposed minimal types (dataclasses or Pydantic models; shown as typing-only sketch):

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional

Json = Mapping[str, Any]

@dataclass(frozen=True)
class MappingSource:
    kind: Literal["explicit", "heuristic", "schema_analyzer_export"]
    fingerprint: Optional[str] = None
    generated_at_iso: Optional[str] = None
    notes: Optional[str] = None

@dataclass(frozen=True)
class MappingBundle:
    # Directly compatible with arangodb-schema-analyzer export (preferred).
    conceptual_schema: Json
    physical_mapping: Json
    metadata: Json

    # Optional artifacts for explainability/reproducibility.
    owl_turtle: Optional[str] = None
    source: Optional[MappingSource] = None
```

### 3.3 Mapping accessors used by front-ends
Front-ends need a stable way to resolve conceptual identifiers into physical forms:

```python
class MappingResolver:
    def __init__(self, bundle: MappingBundle): ...

    def resolve_entity(self, label_or_entity: str) -> Json:
        """Return mapping dict (e.g., style=COLLECTION or LABEL + fields)."""

    def resolve_relationship(self, rel_type: str) -> Json:
        """Return mapping dict (DEDICATED_COLLECTION vs GENERIC_WITH_TYPE)."""

    def validate_query_references(self, *, labels: set[str], rel_types: set[str]) -> list[Json]:
        """Return structured warnings/errors for unknown labels/types."""
```

### 3.4 Acquisition (core provides helpers, not policy)
Core may provide a “mapping acquisition adapter” that calls `arangodb-schema-analyzer`, but policy belongs to the front-end app/service:

```python
def acquire_mapping_bundle(
    *,
    db,  # python-arango Database
    analyzer_options: dict[str, Any] | None = None,
    include_owl: bool = False,
    cache: "MappingCache | None" = None,
) -> MappingBundle:
    ...
```

Notes:
- caching key should incorporate a schema fingerprint (collection/index inventory + analyzer version).
- OWL Turtle is stored as an **artifact** (primarily for explainability), while translation uses export JSON.

---

## 4) AQL builder / emitter API surface

### 4.1 Goals
- Always produce **(aql_text, bind_vars)**.
- Keep collection names injection-safe (`@@collection` bind params).
- Allow composability: fragments can be concatenated safely without string spaghetti.
- Support debug metadata (e.g., source spans, “why this fragment exists”).

### 4.2 Core types

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass
class AqlFragment:
    text: str
    bind_vars: dict[str, Any] = field(default_factory=dict)

    def __add__(self, other: "AqlFragment") -> "AqlFragment":
        """Concatenate text with newline; merge bind vars with collision checks."""

@dataclass(frozen=True)
class AqlQuery:
    text: str
    bind_vars: dict[str, Any]
    debug: dict[str, Any] | None = None
```

### 4.3 Bind var conventions
- `@@name` bind vars for collection/view names.
- `@name` bind vars for values.
- core enforces:
  - no direct interpolation of user strings into AQL keywords, collection names, attribute paths
  - collisions: either auto-rename or raise with a clear error (configurable)

### 4.4 Common builders (suggested)

```python
class AqlBuilder:
    def let(self, var: str, expr: str, *, bind_vars: dict[str, Any] | None = None) -> AqlFragment: ...
    def for_in(self, var: str, collection_bind: str) -> AqlFragment: ...
    def filter(self, expr: str, *, bind_vars: dict[str, Any] | None = None) -> AqlFragment: ...
    def sort(self, exprs: list[str]) -> AqlFragment: ...
    def limit(self, skip: int | None, limit: int | None) -> AqlFragment: ...
    def return_(self, expr: str) -> AqlFragment: ...
```

### 4.5 Integration point with schema mapping
Core should expose helper builders that use the analyzer’s mapping styles:

```python
def aql_match_entity(*, resolver: MappingResolver, var: str, entity: str) -> AqlFragment: ...
def aql_expand_relationship(*, resolver: MappingResolver, from_var: str, rel_type: str, to_var: str, direction: str) -> AqlFragment: ...
```

These provide the building blocks for both:
- Cypher pattern expansion
- SPARQL BGP triple pattern lowering

---

## 5) Extension registry interface (ArangoSearch / vector / geo)

### 5.1 Goals
- Keep extensions namespaced and explicitly enabled.
- Allow both expression-level extensions and source-changing extensions.
- Provide consistent testing hooks (golden AQL and integration execution).

### 5.2 Surface: functions vs procedures

```python
from dataclasses import dataclass
from typing import Any, Callable, Optional

@dataclass(frozen=True)
class CompileContext:
    mapping: MappingResolver
    aql: AqlBuilder
    options: dict[str, Any]

@dataclass(frozen=True)
class CompiledExpr:
    expr: str
    bind_vars: dict[str, Any]
    warnings: list[dict[str, Any]] = ()

@dataclass(frozen=True)
class CompiledProcedure:
    subquery: str
    bind_vars: dict[str, Any]
    yields: list[str]
    warnings: list[dict[str, Any]] = ()

FunctionCompiler = Callable[[Any, CompileContext], CompiledExpr]
ProcedureCompiler = Callable[[Any, CompileContext], CompiledProcedure]

class ExtensionRegistry:
    def register_function(self, name: str, compiler: FunctionCompiler) -> None: ...
    def register_procedure(self, name: str, compiler: ProcedureCompiler) -> None: ...
    def compile_function(self, name: str, call_ast: Any, ctx: CompileContext) -> CompiledExpr: ...
    def compile_procedure(self, name: str, call_ast: Any, ctx: CompileContext) -> CompiledProcedure: ...
```

Notes:
- `call_ast` type is front-end-specific (Cypher AST vs SPARQL algebra). Each front-end provides a small adapter that normalizes “function call” and “procedure call” nodes into the shape expected by compilers.

### 5.3 Capability policy
Registry should be wrapped by policy checks:
- `extensions.enabled: bool`
- `extensions.allowlist / denylist`
- hard-disable “escape hatch” extensions by default (e.g. raw AQL)

---

## 6) Execution adapter API surface

Core provides an executor that runs AQL on `python-arango`:

```python
class AqlExecutor:
    def __init__(self, db): ...
    def execute(self, query: AqlQuery, *, batch_size: int | None = None) -> Any:
        """Return cursor/iterator; front-ends decide how to materialize."""
```

---

## 7) How front-ends use core (alignment contract)

### Cypher front-end
- Parse Cypher → normalize AST
- Compile patterns/clauses into core `AqlFragment`s using:
  - `MappingResolver`
  - mapping-based helpers like `aql_match_entity`, `aql_expand_relationship`
  - `ExtensionRegistry` for `arango.*` and `CALL arango.*`
- Emit final `AqlQuery`

### SPARQL front-end
- Parse SPARQL → SPARQL algebra (or normalized AST)
- Lower triple patterns to:
  - mapping-based entity match + relationship expansion helpers
- Use extension registry for Arango-specific SPARQL extensions (if any)

---

## 8) Versioning and stability
- `arango-query-core` publishes a **semver-stable** API.
- Mapping bundle format should remain compatible with `arangodb-schema-analyzer` export contract; if that contract evolves, core should support multiple versions via adapters.
- Front-ends pin `arango-query-core` minor versions for predictable translation output.

