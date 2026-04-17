Your findings align perfectly with the broader industry consensus: **direct NL-to-AQL performs poorly compared to NL-to-Cypher.** This happens for two main reasons:
1. **Training Data Distribution:** Foundational LLMs (GPT-4o, Claude 3.5, Llama 3) have ingested orders of magnitude more Cypher (thanks to Neo4j and openCypher's market share) than AQL. Cypher is essentially the SQL of graphs for LLMs.
2. **Conceptual vs. Physical Leakage:** AQL is a procedural, multi-model language that requires exact physical schema details (e.g., exact collection names, `_from`/`_to` traversal directions, loop structures). Cypher allows the LLM to operate purely on a **conceptual** level (ASCII art: `()-[]->()`), which consumes far fewer tokens and reduces cognitive load on the LLM.

Your strategy—**predicting Cypher using an LLM and transpiling deterministically to AQL**—is absolutely the State of the Art (SOTA) for multi-model databases like ArangoDB. 

Here is an analysis of the SOTA for this architecture, an evaluation of your `arango-cypher-py` repository, and concrete suggestions to optimize for latency, reliability, and token efficiency.

---

### Part 1: SOTA Analysis for NL-to-Graph Pipelines

In 2025/2026, the industry standard for Text-to-Graph architectures relies on separating the **logical generation** from the **physical execution**, supported by RAG and iterative validation.

1. **Logical Prediction over Physical Prediction:** SOTA systems hide the database's physical storage layer from the LLM. The LLM predicts an abstract Graph Query (Cypher) against an ontology. A deterministic transpiler maps that Cypher to the target database language (AQL, Gremlin, GQL).
2. **Pre-generation Entity Resolution:** LLMs are terrible at guessing exact database strings (e.g., guessing `"Matrix"` instead of `"The Matrix"`). SOTA pipelines intercept the user's prompt, extract named entities, run a fuzzy/vector search against the database, and inject the *exact* database strings into the LLM prompt.
3. **Dynamic Few-Shot Prompting:** Instead of relying purely on zero-shot generation (which requires multiple retries/corrections, eating up tokens and latency), SOTA systems use a vector database to retrieve 3-5 historical, validated "NL → Cypher" examples similar to the user's current question and inject them into the system prompt.
4. **Execution-Grounded Self-Healing:** Validating syntax isn't enough. SOTA agents do a "dry-run" (using an `EXPLAIN` plan). If the database throws a semantic error (e.g., "Property does not exist on Label"), the error is fed back to the LLM to heal the query before showing the user.
5. **Small Language Models (SLMs):** For ultra-low latency and low token costs, enterprises are moving away from GPT-4 class models to fine-tuned 7B/8B parameter models (like Llama 3 or Qwen 2.5) specialized entirely on their specific conceptual schema.

---

### Part 2: Analysis of `arango-cypher-py` vs. SOTA

Your repository is remarkably well-architected and already implements several SOTA concepts. 

**What you are doing exceptionally well:**
* **The Architecture:** Using Cypher as an Intermediate Representation (IR) and utilizing a deterministic AST-to-AST transpiler (`translate_v0.py`) is exactly the right approach.
* **Token Efficiency:** In `nl2cypher.py` (`_build_schema_summary`), you are intentionally stripping out physical mapping details (collection names, type discriminator fields) and only feeding the LLM the conceptual schema. This is brilliant for saving tokens.
* **Self-Healing Loop:** Your `_call_llm_with_retry` method implements a syntax-based healing loop using the ANTLR parser. If the LLM generates invalid Cypher, you feed the specific parser error back into the LLM.
* **Rule-Based Fallback:** The `_rule_based_translate` function is a great pragmatic fallback that uses regex/heuristics for common queries (zero tokens, sub-millisecond latency).

**Where the repository falls short of SOTA:**
* **Zero-Shot Vulnerability:** Your `_SYSTEM_PROMPT` in `nl2cypher.py` contains the schema but **no examples**. Zero-shot Cypher generation frequently hallucinates relationship directions or uses functions the transpiler doesn't support yet, forcing the retry loop to activate (which doubles latency and token usage).
* **Missing Entity Resolution:** The repo currently relies on the LLM to guess exact string values from the user prompt.
* **No Semantic Validation:** The retry loop (`_validate_cypher`) only checks if the Cypher is syntactically valid via ANTLR. It does not check if the query makes logical sense against the actual ArangoDB data.

---

### Part 3: Suggestions for Improvement (Optimizing for Latency, Reliability & Cost)

To satisfy the customer's strict latency, reliability, and token usage requirements, I recommend the following upgrades to `arango-cypher-py`:

#### 1. Implement Dynamic Few-Shot Prompting (High Impact, Low Effort)
**The Problem:** Generating Cypher zero-shot often leads to syntax/logic errors, triggering your `max_retries` loop. This multiplies token usage and adds seconds to latency.
**The Fix:** * Create a lightweight local vector store (or even just an in-memory embedding comparison) containing 50-100 validated NL-to-Cypher pairs based on your schema.
* In `nl_to_cypher`, embed the user's question, retrieve the top 3 most similar examples, and inject them into `_SYSTEM_PROMPT`. 
* **Why:** The LLM will copy the structure of the examples. This will drastically increase your "first-shot" success rate, bypassing the retry loop, cutting token usage, and halving latency.

#### 2. Add Pre-Flight Entity Resolution (High Impact, Medium Effort)
**The Problem:** If a user asks *"Who acted in Forest Gump?"*, the LLM might write `WHERE m.title = 'Forest Gump'`. But if the database stores `"Forrest Gump"`, the generated AQL will return an empty set.
**The Fix:**
* Before calling the LLM, run a fast Named Entity Recognition (NER) pass or use a cheap, tiny LLM call to extract entities.
* Query ArangoDB (using ArangoSearch/BM25) to find the exact string matches.
* Inject a mapping into the LLM prompt: `User mentioned 'Forest Gump'. Exact database match: 'Forrest Gump'`.
* **Why:** Improves reliability from the user's perspective (they get actual data back) without requiring complex query rewriting later.

#### 3. Upgrade to "Execution-Grounded" Validation (Medium Impact, Medium Effort)
**The Problem:** Your ANTLR parser only verifies Cypher grammar. It will happily approve `MATCH (n:Dog)-[:MEOWS]->(c:Cat)` even if `MEOWS` doesn't exist in the schema.
**The Fix:**
* Inside your `_call_llm_with_retry` loop, after validating the Cypher syntax, immediately run it through your `translate()` function.
* Take the resulting AQL and send it to ArangoDB's `_api/explain` endpoint.
* If ArangoDB returns a parsing error or indicates a missing collection/index, pass *that* physical error back to the LLM as feedback. 
* **Why:** Guarantees that the query returned to the customer will actually execute reliably on the database.

#### 4. Token Optimization: Cache the Schema (Low Effort)
**The Problem:** You are sending the entire `schema_summary` (which can be thousands of tokens for large DBs) on every single request.
**The Fix:** Modern APIs (like Anthropic's Claude 3.5 Sonnet or OpenAI's Prompt Caching) allow you to cache the system prompt. Since the conceptual schema rarely changes, utilize API prompt caching. This can reduce input token costs by up to 90% and lower Time-To-First-Token (TTFT) latency by hundreds of milliseconds.

#### 5. Ultimate Performance: Fine-tune an SLM (High Effort, Massive Reward)
**The Problem:** Hosted frontier models have high variance in latency.
**The Fix:** Since your transpiler only supports a specific subset of Cypher (as noted in `README.md`), use an LLM to generate 10,000 NL-to-Cypher pairs based on your `query-corpus.yml`. Fine-tune a small, fast model (like `Llama-3-8B-Instruct`). 
* **Why:** A fine-tuned SLM hosted locally or on dedicated hardware will generate Cypher in <200ms, use a fraction of the tokens, and naturally adhere to your specific conceptual schema and supported Cypher subset without needing a massive prompt.