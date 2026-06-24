# Query Workbench Shell — UI Specification

**Status:** Proposed
**Audience:** `arango-cypher-py` and `arango-sparql-py` (and any future NL→query
workbench that targets ArangoDB).
**Goal:** A shared, language-agnostic UI shell that hides the "gory details" of
query translation behind a **chat-first, progressive-disclosure** interface,
while keeping every power feature one click away.

This document is intentionally generic. Each repo implements the same shell and
supplies a small set of language bindings (see §9 and each repo's addendum).

---

## 1. Design principles

1. **Chat-first.** The default surface is a conversational composer + a results
   panel. A user types a question, presses Send, and gets answers. Nothing else
   is visible until asked for.
2. **Progressive disclosure.** Source/target query editors, the schema/mapping
   panel, and the toolbar of power actions are **hidden by default** and revealed
   on demand.
3. **Context over toolbars.** Power features are surfaced as **per-result
   affordances** (chips under each answer), not as a permanent wall of buttons.
4. **Errors never dead-end.** Any pipeline failure surfaces inline with a
   concrete next action and (configurably) auto-opens the relevant editor.
5. **Reuse, don't reinvent.** The collapsible+resizable panel pattern, the
   `localStorage`-persisted preference pattern, and the results panel already in
   these apps are the building blocks.
6. **No native dialogs.** Never `window.confirm/alert/prompt`. Use inline
   affordances, in-app overlays, and toasts. (Consistent with each repo's
   `ui-architecture` rule §18.)

> **Conscious divergence:** `ui-architecture.mdc` §8 prefers zones that are
> "resizable, never collapsed to zero." A focused query tool benefits from
> progressive disclosure instead, so this shell *does* allow closing the editor
> and schema panels. This is a deliberate, documented exception scoped to the
> query workbench; §18 (no native dialogs) and the resize ergonomics still hold.

---

## 2. Three levels of disclosure

| Level | Surface | Default | How revealed |
| --- | --- | --- | --- |
| **L0 Conversation** | Chat composer + Results panel | **Visible** | — |
| **L1 Query Inspector** | Source `|` Target editors + power actions | **Hidden** | Inspector toggle, a result chip, or auto-open-on-failure |
| **L2 Workspace panels** | Schema/Mapping, Outline, Samples, History, Settings | **Hidden** | Gear menu |

The header collapses to three things: **product title**, **connection control**,
and a **gear** (settings) button.

---

## 3. The conversation (L0)

### 3.1 Composer

- Multi-line text input styled like an agent chat box, with a **Send** button
  (paper-plane) and a busy/stop state.
- **Keyboard:** `Enter` = Send; `Shift+Enter` = newline. (This *replaces* any
  prior "Enter generates only / Shift+Enter runs" mapping.)
- Disabled Send when empty or a turn is in flight; Send shows a cancel control
  while the pipeline runs.
- Optional context chips rendered inline (e.g. active tenant, active graph
  scope) so the user always sees the query context.

### 3.2 The default action (Send)

`Send` runs the **full pipeline** end to end, with graceful degradation:

| Connection / outcome | Behavior |
| --- | --- |
| Connected, all OK | NL → source query → (transpile to target) → execute → results |
| **Disconnected** | NL → source → transpile, then open Inspector + inline "Connect to run" (no execution) |
| **Transpile fails** | Inline error + the AI-fallback action (if the repo has one) + open Inspector at the source editor |
| **Execution fails** | Inline error + open Inspector at the target editor with the error highlighted |

### 3.3 Pipeline status

While a turn runs, show an ordered, legible status strip reflecting the repo's
pipeline stages, e.g.:

```
Generating <source>…  →  Transpiling…  →  Running…  →  ✓ 12 rows (243 ms)
```

Each stage can independently show success/skip/error. Stages a repo does not
have (e.g. SPARQL with no transpile step) are simply omitted.

### 3.4 Per-result affordance bar

Under each completed turn, render a compact row of chips. Each opens the
Inspector/Results focused on **that turn's** artifacts:

`Source` · `Target` · `Explain` · `Profile` · `Graph` · `Edit`

- Only show chips that apply (e.g. `Profile`/`Explain` require a connection;
  `Target` only when a transpile step exists).
- This is the discoverability mechanism that lets every button be hidden by
  default without burying the power features.

### 3.5 Results panel

- A **single persistent panel** showing the **active turn's** results (initial
  scope — not inline-per-turn).
- Tabs: **Table** (default), **JSON**, **Graph**, plus **Explain** / **Profile**
  when present.
- **Lazy-mount** heavy tabs (graph canvas, large tables) only when selected.
- Export (CSV/JSON), row count, and timing live here.

---

## 4. Query Inspector (L1)

A collapsible drawer, **closed by default**, that contains the previously-toolbar
power surface.

- **Placement:** bottom drawer, above the results panel (keeps the natural
  composer → editors → results vertical flow). Repos may override.
- **Contents:**
  - A **split of source `|` target editors** (e.g. Cypher | AQL, SPARQL | —).
  - A **movable vertical divider** between the two editors.
  - **Each side independently closable** (collapse to a labeled rail, like the
    existing schema-panel rail). If a repo has only one editor (no transpile),
    the split degrades to a single pane.
  - The **power actions**: Translate/Transpile, Run, Explain, Profile, plus the
    manual-flow toggles (Auto-translate, Auto-run) and the source/target
    generation buttons.
- **Edit round-trip:** opening the Inspector loads the active turn's source and
  target queries. Editing + re-running flows results back to the shared results
  panel and updates the active turn. Any "learn / save correction" feature stays
  available here.
- **Resize:** the drawer height and the source/target divider are drag-resizable
  and persisted.

Reuse the existing collapse+resize+rail implementation (the schema/mapping panel
already does width-collapse + drag-resize + a vertical rail button).

---

## 5. Settings (L2, the gear)

A gear icon in the header opens a popover holding **global preferences** and the
**workspace panels**:

- **Panels:** Schema/Mapping, Clause/Query Outline, Samples, History.
- **Preferences (persisted to `localStorage`):**
  - NL mode (two-stage via source language vs. direct-to-target), where
    applicable.
  - Auto-run on Send (on/off) and disconnected behavior (generate-only).
  - Default results tab; table density/theme.
  - Auto-open Inspector on failure (default: on).
- Keep the popover keyboard-reachable; every item also has a stable home (don't
  make the gear the *only* path to, e.g., History if a chip can open it).

---

## 6. Keyboard & accessibility

- `Enter` send, `Shift+Enter` newline (composer). `Esc` closes the active
  overlay/drawer.
- Power-user re-run lives in the Inspector (e.g. `Cmd/Ctrl+Enter` to re-run the
  edited query). Document shortcuts in a discreet help affordance, not a
  permanent legend.
- ARIA roles for the composer, status strip (`role="status"`), result chips, and
  drawers; focus moves into a drawer when opened and returns on close.

---

## 7. State model (incremental)

**Phase-1 scope:** keep the existing **single active query** state. The
conversation is a chat-*styled* composer plus a lightweight status/message log;
the results panel reflects the single active turn. This avoids a large store
refactor while delivering the chat feel.

**Later (optional):** a true multi-turn transcript (a list of turns, each with
its question/source/target/results/errors), enabling scrollback and re-running
prior turns. Designed for but not required by Phase 1.

---

## 8. Persistence & deep-linking

- All disclosure toggles and preferences persist to `localStorage` (matches the
  existing pattern), keyed per connection where it matters.
- Optional: encode the active question / open-panel state in URL query params for
  shareable links (no new routes).

---

## 9. Language bindings (what each repo supplies)

The shell is parameterized by:

| Binding | `arango-cypher-py` | `arango-sparql-py` |
| --- | --- | --- |
| `sourceLanguage` | Cypher | SPARQL |
| `targetLanguage` | AQL | AQL (or none, if executing SPARQL directly) |
| `pipeline` | NL→Cypher→AQL→execute (two-stage) | NL→SPARQL→execute (one-stage) or NL→SPARQL→AQL |
| `hasTranspileStep` | yes (Cypher→AQL) | repo decision |
| `aiFallback` | "Generate AQL with AI" on transpile gap | repo decision |
| `editors` | Cypher + AQL (CodeMirror) | SPARQL (+ optional target) |
| `resultGraphMapping` | nodes/edges → graph tab | triples/bindings → graph tab |
| `schemaPanel` | mapping panel | ontology/prefixes panel |

Everything else — composer, pipeline-status model, results panel, Inspector
drawer mechanics, affordance bar, gear/settings, failure-degradation table,
keyboard model — is **identical** and should be implemented the same way in both
repos (ideally copy-compatible components).

---

## 10. Phased rollout (per repo)

Each phase is independently shippable; build + unit tests run after each.

- **Phase 0 — Settings scaffold.** Add the gear popover; relocate
  Samples/History/Outline/Schema and the auto toggles + NL-mode into it. Header
  shrinks to title + connection + gear. *No behavior change.*
- **Phase 1 — Chat composer.** Restyle the NL bar into a chat composer
  (multi-line, Send, `Enter`/`Shift+Enter`, status strip). Wire Send to the full
  pipeline with the §3.2 degradation table. Keep single-active-query state.
- **Phase 2 — Query Inspector drawer.** Wrap the source|target editors + power
  actions into a collapsible bottom drawer, **closed by default**; add the
  movable divider + per-side close. Reuse the existing collapse/resize/rail
  pattern.
- **Phase 3 — Affordances & polish.** Per-result chip bar; lazy-mount the graph
  canvas and editors; auto-open-Inspector-on-failure.
- **Phase 4 (optional) — Multi-turn transcript.**

---

## 11. Open/again-confirm decisions (locked for arango-cypher; revisit per repo)

- State model: chat-styled now, full transcript later. ✓
- Results: single persistent panel (active turn), not inline-per-turn. ✓
- Inspector placement: bottom drawer above results. ✓
- Direct NL→target mode: kept, but behind the gear. ✓

`arango-sparql-py` should confirm these four for its own pipeline shape (it may
have no transpile step, which simplifies the Inspector to a single pane).
