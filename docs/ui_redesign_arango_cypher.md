# arango-cypher UI Redesign — Addendum to the Query Workbench Shell

This is the repo-specific companion to `docs/query_workbench_shell_spec.md`. It
maps the generic shell onto the concrete components, state, and handlers in
`ui/src` and gives a file-level plan for each phase.

## Language bindings

| Binding | Value |
| --- | --- |
| `sourceLanguage` | Cypher (`CypherEditor`) |
| `targetLanguage` | AQL (`AqlEditor`) |
| `pipeline` | NL→Cypher→AQL→execute (two-stage); also direct NL→AQL |
| `hasTranspileStep` | yes — `handleTranslate` (Cypher→AQL) |
| `aiFallback` | `handleFallbackToAql` ("Generate AQL with AI") on transpile gap |
| `resultGraphMapping` | rows with `{nodes, edges}` → `ResultsPanel` Graph tab (Cytoscape) |
| `schemaPanel` | `MappingPanel` |

## Current → target mapping

| Today (in `App.tsx`) | Target |
| --- | --- |
| Header: title, `ConnectionDialog`, `GraphSelector`, `TenantSelector`, `Samples`, `History`, `Outline`, `Mapping` | Header: title, `ConnectionDialog`, **gear**. Graph/Tenant selectors stay (they're connection context) or move into the connection area. |
| `Ask:` input bar (`Enter`→`handleNL`) + NL `Cypher`/`AQL` toggle + `Generate` | **Chat composer** (multi-line, Send=full pipeline, `Enter`/`Shift+Enter`). NL-mode toggle → gear. |
| Editor toolbar: `Translate` `Run` `Explain` `Profile` `Auto-translate` `Auto-run` | Moves **into the Inspector drawer** (hidden by default). |
| Side-by-side Cypher \| AQL with fixed `border-r` | **Inspector drawer** (closed by default): movable divider + per-side collapse, reusing the schema-panel rail pattern. |
| `ResultsPanel` (Table/JSON/Graph + Explain/Profile), fixed `h-64` | Same panel; **lazy-mount** Cytoscape; height becomes drawer-aware. |
| `MappingPanel` (already collapsible+resizable) | Reachable from the gear; keep collapse/resize/rail. |
| Banners: `nlError`, `nlAdvisories`, schema-pending, error w/ `handleRegenerateFromNl`/`handleFallbackToAql` | Become **inline turn messages / affordances** in the conversation (advisory "Create index" stays an inline action). |

## Reusable patterns already in the repo

- **Collapse + resize + vertical rail:** `showMapping` / `mappingWidth` / the
  `onMouseDown` drag handle / the collapsed `Mapping` rail button. Clone this for
  the Inspector and the Cypher\|AQL split.
- **Persisted toggles:** `autoTranslate` / `autoRun` / `nlHistory` read/write
  `localStorage` via lazy `useState` initializers. Use the same for new prefs.
- **Pure helper + unit test:** `ui/src/utils/warnings.ts` (+ `.test.ts`) is the
  template for extracting logic (e.g. a `pipeline` reducer/helper) testable under
  vitest without a DOM harness.

## Phase plan (file-level)

### Phase 0 — Settings scaffold (no behavior change)
- New `ui/src/components/SettingsMenu.tsx` (gear popover).
- Move `Samples`, `History`, `Outline`, `Mapping` triggers and the
  `Auto-translate` / `Auto-run` / NL-mode controls into it.
- `App.tsx` header shrinks to title + `ConnectionDialog` + gear (+ graph/tenant
  selectors as connection context).
- Persist any new open/pref state in `localStorage`.
- Verify: `npm run build`, `npx vitest run`, `test_ui_dist_freshness`.

### Phase 1 — Chat composer
- New `ui/src/components/ChatComposer.tsx`: `<textarea>` auto-grow, Send button,
  busy/cancel, `Enter`=send / `Shift+Enter`=newline, status strip
  (`role="status"`).
- New `ui/src/utils/pipeline.ts` (pure): given `{connected, nlMode, autoRun}` →
  the ordered stage plan + the §3.2 degradation outcome. Unit-tested.
- Wire Send to orchestrate `handleNL` → `handleTranslate` → `handleExecute`
  (reuse existing handlers; do not duplicate their logic). Keep single active
  query in `store.ts`.
- Add a lightweight per-turn status/message list (not a full transcript).

### Phase 2 — Query Inspector drawer
- New `ui/src/components/QueryInspector.tsx`: bottom drawer, closed by default,
  containing the two editors + the relocated power actions.
- Movable Cypher\|AQL divider (new `splitRatio` state, drag handle) + per-side
  collapse rails. Drawer height resizable; persist `splitRatio` + drawer height.
- `handleLearn` (save corrected AQL) stays in the AQL side.

### Phase 3 — Affordances & polish
- Per-result chip bar component: `Cypher` `AQL` `Explain` `Profile` `Graph`
  `Edit`, each opening the Inspector/Results focused appropriately.
- Lazy-mount `CytoscapeGraph` (only when the Graph tab is active) and the editors
  (only when the Inspector is open).
- Auto-open the Inspector at the failing stage on transpile/exec error
  (preference-gated).

### Phase 4 (optional) — Multi-turn transcript
- Evolve `store.ts` to a list of turns; results panel reflects the selected turn.

## Risks / watch-items specific to this repo

- `App.tsx` is ~1850 lines and near the 1500-line modularity cap. Each new
  surface (`SettingsMenu`, `ChatComposer`, `QueryInspector`) should be its own
  component to *reduce* `App.tsx`, not grow it.
- `handleExecute` already handles both Cypher-and-AQL and AQL-only paths; the
  composer must call it the same way to preserve learned-correction and
  tenant-scope behavior.
- Keep the `nlAdvisories` "Create index" flow working (now an inline turn
  affordance).
- Don't regress keyboard handling for users mid-edit in the editors (the new
  `Enter`=send only applies to the composer, not the CodeMirror editors).
