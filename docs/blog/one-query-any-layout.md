# One Query, Any Layout: Bringing Cypher — and Plain English — to ArangoDB

*How a small Python engine lets you ask a graph database questions without knowing how its data is physically stored — or even knowing a query language at all.*

---

## The problem hiding inside every graph database

Imagine you have a database full of connected things — people who know people, companies that own companies, parts that go into products. This is what **graph databases** are for. Instead of storing data in rectangular tables like a spreadsheet, they store **nodes** (the things) and **edges** (the relationships between them). Asking "who are the friends-of-friends of Alice who live in Berlin?" is a natural, fast question for a graph database and an awkward one for a traditional table-based database.

There's a catch that almost nobody outside the field talks about: **the graph world never agreed on one language.**

The most popular graph query language is **Cypher** — the language that made the graph database Neo4j famous. It reads almost like English: `MATCH (a:Person)-[:KNOWS]->(b:Person) RETURN b.name`. There's a huge amount of Cypher out there: tutorials, Stack Overflow answers, and — crucially for what follows — it's the graph language that large language models like ChatGPT are best at writing, because they've seen so much of it.

But not every graph database *speaks* Cypher. **ArangoDB** — a popular, flexible, open-source database — speaks its own language called **AQL** (ArangoDB Query Language). AQL is powerful, but the world has written far less of it, and today's AI models are noticeably weaker at producing it.

So if your data lives in ArangoDB, you face a frustrating gap: the query language everyone knows and every AI writes well (Cypher) isn't the one your database understands (AQL).

`arango-cypher-py` is a Python project that closes that gap — and, in doing so, solves a second problem most people don't even realize they have.

---

## The second problem: the same data can be arranged a dozen ways

Here's the subtle part. Even within a single database like ArangoDB, **there is no single "correct" way to physically store a graph.** The same conceptual idea — "People who KNOW other People" — can be laid out in the database in genuinely different ways:

- **One collection per type** (all People in a `people` table, all "knows" relationships in a `knows` table). Clean and explicit.
- **One big generic collection with a "type" label** (all nodes together in one table, each tagged with what it *is*). Flexible, and common in AI-generated knowledge graphs where you don't know in advance what types will show up.
- **A hybrid** — some things stored the first way, some the second.

A query that works against one layout will silently fail against another, because the physical names — the actual table names, the fields, the way relationships are stored — are all different. Traditionally, this means whoever writes the query has to *know the physical storage layout by heart* and hand-tailor every query to it.

That couples your questions to your plumbing. Change the plumbing, and every query breaks.

---

## The core idea: separate *what you're asking* from *how it's stored*

`arango-cypher-py` draws a hard line between two things that are usually tangled together:

- The **conceptual schema** — the human-meaningful vocabulary: "Person," "Company," "KNOWS," "OWNS." This is what you write queries against.
- The **physical mapping** — the messy reality of which table holds what, which field marks a node's type, how edges are actually stored.

You write your query in conceptual terms — `Person`, `KNOWS` — and a **mapping layer** translates those concepts into whatever the physical database actually looks like. Change the storage layout, update the mapping, and *the same query keeps working.* Your questions become portable across every possible layout.

This is the quiet superpower of the whole system: **one Cypher query runs unchanged against any physical arrangement of the data.**

---

## Two pipelines, one engine

The project offers two ways in, and they share the same machinery.

### Pipeline 1: Cypher in, AQL out — deterministically

You hand it a Cypher query. It parses that query, consults the mapping (conceptual → physical), and emits the equivalent AQL plus a set of safely-separated parameters. No AI is involved anywhere in this path. The same Cypher and the same mapping always produce exactly the same AQL — every single time.

That determinism matters more than it sounds. It means the translation is *auditable* and *testable*: you can pin the expected output in a test and trust it forever. It's a compiler, not a guess.

### Pipeline 2: Plain English in, results out

This is the one that turns heads. You ask a question in ordinary language — *"Who acted in Forrest Gump?"* (typo and all) — and the system:

1. Uses a large language model to turn your question into **conceptual Cypher** — and here's the key design decision: **the AI only ever sees the human-meaningful vocabulary, never the physical storage details.**
2. Feeds that Cypher into the deterministic translator from Pipeline 1 to get executable AQL.
3. Runs it and hands you the answer.

Why go through Cypher at all, instead of asking the AI for AQL directly? Because of that training-data gap. **LLMs write good Cypher and shaky AQL.** By having the model produce the language it's fluent in, and letting a deterministic engine handle the language it isn't, you get the best of both: the AI's natural-language understanding *and* an exact, trustworthy translation. The AI does the creative part it's good at; the compiler does the precise part it's good at. Neither is asked to do the other's job.

---

## The details that make it trustworthy

A demo that works once is easy. A system you'd put in front of real users needs guardrails. A few worth calling out, in plain terms:

**It never pastes your words into a query.** User input always travels as separate, safely-bound parameters — never glued directly into the query text. This is the graph-database equivalent of preventing "SQL injection," the classic attack where a malicious input rewrites the query itself.

**It refuses to leak between customers.** If you deploy this as a shared service where many tenants share one database, the scariest failure is one customer accidentally seeing another's data. The system makes that **structurally impossible** rather than merely unlikely: every query is provably confined to the caller's own data, checked at multiple independent layers, and any query that can't be proven safe simply **doesn't run**. Notably, the AI is never *trusted* to enforce this — a separate, deterministic check has the final say, so a clever prompt can't talk its way past the boundary.

**It grounds the AI in reality.** Before the model writes a query, the system can look up the real names that actually exist in your database and feed them to the model — so it filters on values that are really there instead of plausible-sounding inventions. If your question has a typo, it can still find the right match. And when the generated query would fail, the system feeds the database's own error back to the model and lets it try again.

**It learns from corrections, locally.** If you fix a bad translation, that correction is remembered and reused for similar future questions — and it all stays on your own machine.

---

## Who is this for?

- **Teams migrating from Neo4j to ArangoDB** who have piles of existing Cypher and don't want to rewrite it all by hand.
- **Developers who want a "just ask in English" layer** over their graph data — for an internal tool, a notebook, or an AI agent — without hand-writing database queries.
- **Anyone building on ArangoDB** who wants their queries to survive changes to how the data is physically stored.
- **AI agents**, which get a stable, predictable set of tools for translating and explaining queries rather than being asked to improvise raw database code.

---

## Where it stands

The project is real and working: a deterministic Cypher-to-AQL translator covering a broad slice of the Cypher language, the natural-language pipeline described above, a browser-based workbench for experimenting, and a growing test suite that cross-checks its translations against Neo4j itself — running the same queries against both engines and comparing the results row by row. It's an active, evolving codebase, deliberately built so the boundaries between "the AI's job" and "the deterministic engine's job" never blur.

---

## The takeaway

The clever move here isn't any single piece of technology — it's a **separation of concerns** applied twice:

1. Between **what you ask** and **how it's stored**, so your queries outlive your storage decisions.
2. Between **the AI that understands your intent** and **the deterministic engine that executes it exactly**, so you get natural language without giving up correctness.

That's a pattern worth stealing well beyond graph databases: let the language model do the fuzzy, human-facing part it's genuinely good at, and let a boring, deterministic, testable engine handle the part where being *exactly right* is non-negotiable. Put a clean contract between them, and you get a system that's both approachable and trustworthy — a combination that's rarer than it should be.

---

*`arango-cypher-py` is an open-source Python project. If you work with graph data — or you're just interested in how to safely put a large language model in front of a real database — it's a compact, readable example of doing it right.*
