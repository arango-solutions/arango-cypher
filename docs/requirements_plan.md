> **Superseded:** This document is **historical context** only. The authoritative **product requirements** live in **[`python_prd.md`](./python_prd.md)** (see §10 for the unified roadmap). The **detailed implementation plan** with work packages and dependencies lives in **[`implementation_plan.md`](./implementation_plan.md)**. Prefer editing those documents for planning changes.

---

I would like to create a python implementation of the arango-cypher project (~/code/arango-cypher) that takes advantage of python's capabilities and the large number of python libraries.  would you think very carefully about this and come up with a plan for implementing the fuctionality in python.  obviously it would not be installed on the coordinator like foxx based arango-cypher.  should it be a new project?  how should we name it?  should we also rename this project.  since it is going to be in python could it optionally support agentic workflow?  for schema detection, I would like you to use arango-schema-mapper which is at ~/code/arango-schema-mapper.  This will return a conceptual schema mapped to the physical schema and it will be in the owl ttl format.  this can be used when the algorithmic detection does not match PG or LPG and it will tell you which parts of the graph follow which physical patterns - the cypher to AQL transpiler will need to accomopdate hybrid structured physical arango models.  SHould it have it's own UI for running cypher

Recommendation: make this a new Python project (don’t rename arango-cypher yet)
Why a new project
Runtime model is fundamentally different: Foxx runs inside ArangoDB coordinators; Python will run externally (service/CLI/library).
Dependency surface: Python can use rich parsing/LLM/data tooling without worrying about Foxx constraints.
Migration path: keep arango-cypher stable while the Python stack matures; later you can decide whether to deprecate Foxx or keep both.
Naming (resolved 2026-04-17)
New project name: arango-cypher-py. The suffix is symmetric with the renamed Foxx repo (`arango-cypher-foxx`), honest about the package being a Python distribution, and leaves the bare `arango-cypher` name free on the `arango-solutions` org for a potential future umbrella/spec repo.
PyPI package: arango-cypher-py.
Repo: arango-solutions/arango-cypher-py (GitHub rename from `arango-solutions/arango-cypher` pending org-admin action; `pushurl` and URLs will be updated once it lands).
The legacy JS/Foxx repo previously called `arango-cypher` was renamed to `arango-cypher-foxx` on GitHub on 2026-04-17.
Core architectural goal (Python)
Build a Python schema-aware Cypher→AQL transpiler that supports:
PG (types-as-collections)
LPG (types-as-labels via a generic collection + type field)
Hybrid (some types/relationships are PG, some are LPG, mixed across the same query)
And when schema is not clearly pure PG/LPG, fall back to arango-schema-mapper for hybrid mapping, returning OWL TTL that describes conceptual↔physical mapping.
Key design choice: make it 3 layers
1) Schema detection + mapping layer
Fast path: algorithmic detection (pure PG / pure LPG)
Fallback: call arango-schema-mapper when hybrid/uncertain
Output: OWL TTL
Must be parsed into an internal mapping model usable by the translator
2) Conceptual schema + physical mapping model (in Python)
Define internal dataclasses (language-agnostic model) representing:
Conceptual types (entity types, relationship types)
Physical implementations
Entity: COLLECTION vs LABEL(type_field/type_value in generic collection)
Relationship: DEDICATED_EDGE_COLLECTION vs GENERIC_EDGE_WITH_TYPE(type_field/type_value)
Hybrid partitioning: per-type/per-rel mapping; allow mixtures
3) Cypher→AQL translation layer
Parse Cypher into an AST (Python parser)
Resolve every label / rel-type reference against the conceptual model
Emit AQL using the physical mapping for:
node scans / filters
edge traversals / joins
hybrid traversals (e.g., PG vertex → LPG edge → LPG vertex)
Agentic workflow support (optional)
Yes, but treat it as optional enrichment, not core correctness:
With arango-schema-mapper in place, you may not need an LLM for mapping.
Where agentic helps:
Explain mappings (human-readable docs)
Suggest improvements (indexes, pattern warnings)
Resolve ambiguous conceptual naming or “best effort” mapping when TTL is incomplete
Keep it behind a feature flag and separate module (arangocypher.agentic).
How arango-schema-mapper fits (your requirement)
Contract
Input: database connection / metadata (whatever arango-schema-mapper expects)
Output: OWL TTL describing conceptual schema mapped to physical patterns, including which parts follow PG vs LPG vs hybrid.
What we must implement in Python
Runner for arango-schema-mapper
Prefer importing it as a Python library if possible.
Otherwise shell out (CLI) with robust error handling.
TTL parser
Use rdflib to parse TTL.
Extractor
Convert RDF graph into internal ConceptualSchema + PhysicalMapping.
Mapping queries
Provide a small query API:
resolve_entity(label) -> PhysicalEntityMapping
resolve_relationship(type) -> PhysicalRelationshipMapping
list_supported_labels/types
Validation
Sanity-check that referenced collections/fields exist in ArangoDB (optional but recommended).
Concrete phased plan (Python implementation)
Phase 0 — Repo bootstrap (1–2 days)
Create new repo arango-cypher-py
Package layout (suggestion):
arango_cypher/  __init__.py  cli.py  config.py  schema/    __init__.py    detect.py              # PG/LPG heuristics    mapper_runner.py       # calls arango-schema-mapper    owl_ttl.py             # rdflib parse + extract mapping    model.py               # dataclasses ConceptualSchema/PhysicalMapping    cache.py               # fingerprint + TTL cache  cypher/    parser.py              # wrapper around chosen Cypher parser    ast.py                 # normalized AST types (if needed)  transpiler/    __init__.py    translate.py           # main entrypoint    emit_aql.py            # AQL builder    hybrid.py              # hybrid-specific translation helpers  agentic/ (optional)tests/pyproject.tomlREADME.md
Phase 1 — Decide parsing strategy (critical) (2–4 days)
Pick one:
Use an existing Cypher parser (preferred)
Goal: produce AST with MATCH patterns, WHERE, RETURN, aggregations, path patterns.
If not available/insufficient: implement a subset parser (but that’s risky/slow).
Deliverable: parse “known supported subset” into an AST your emitter can consume.
Phase 2 — Implement schema mapping pipeline (1–2 weeks)
Implement PG/LPG heuristic detector (fast)
Integrate arango-schema-mapper fallback:
run mapper
parse TTL via rdflib
extract mapping dataclasses
Implement caching:
fingerprint schema (collections + indexes + sampled keys + mapper version)
TTL cache on disk (SQLite or files) and optional Redis later
Deliverable: get_schema_mapping(db) -> PhysicalMapping returning pure or hybrid mapping.
Phase 3 — Hybrid-aware translation (2–4 weeks)
Build translation around mapping primitives:
node match AQL fragment generation per conceptual type
relationship traversal generation per relationship type
Ensure hybrid works:
label resolution across mixed storage
relationship type resolution across mixed edge storage
cross-pattern traversal
Add query validation against conceptual schema (helpful errors)
Deliverable: end-to-end translation for core MATCH/WHERE/RETURN for pure and hybrid mappings.
Phase 4 — Hardening + docs + optional agentic (1–2 weeks)
Add fixtures for:
GraphRAG hybrid schema
high-cardinality edges (dedicated + generic)
structured + unstructured mix
Add performance benchmarking
Add agentic “explain mapping” (optional)
Deliverable: v0.1 usable CLI + library API.
Immediate questions I’ll answer by inspecting ~/code/arango-schema-mapper
To make the plan exact (not hand-wavy), I need to confirm:
How to invoke it (library vs CLI)
What TTL vocabulary it emits (classes/properties)
How it encodes PG/LPG/hybrid partitions (per-type triples? per-collection annotations?)
If you want, I can now read that repo and produce:
a concrete TTL-to-dataclass mapping spec
the exact SPARQL/RDF queries (rdflib) to extract entity/relationship mappings
the translation hooks the transpiler will need to support hybrid partitions