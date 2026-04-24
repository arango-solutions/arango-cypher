# Code-quality audits

Periodic whole-tree sweeps for security, dryness, dead code, test-coverage
gaps, CI drift, and docs-to-code drift. One file per audit, dated.

## Convention

- **Filename:** `YYYY-MM-DD-<short-slug>.md` (e.g., `2026-04-28-post-hardening-audit.md`).
- **Structure:** TL;DR table (severity × one-line finding), per-finding sections with `Where / What / Fix / Estimate`, a "what stayed the same" back-reference table, and a prioritised next-actions list.
- **Severity:** H (exploitable-by-unauthenticated or data-integrity loss) / M (authenticated elevation or silent regression) / L (hygiene / paper-cut).
- **Scope:** each audit should declare explicitly what it looked at and what it didn't.

## History

| Audit | Date | Findings | Status |
|---|---|---|---|
| [`2026-04-28-post-hardening-audit.md`](./2026-04-28-post-hardening-audit.md) | 2026-04-28 | 9 (0 H, 3 M, 6 L) | Open — PR #13 landed the doc; fixes are queued as separate PRs per the doc's next-actions list. |
| Wave-6a audit (in-chat only, not persisted here) | ≈ 2026-04-23 | 10 TL;DR items | Closed — all ten items merged across PRs #7 / #8 / #9 / #10 / #11 / #12. See the `docs/python_prd.md` changelog entries from 2026-04-24 through 2026-04-28. |

Historical audits that happened before this folder existed (the Wave-6a
audit referenced above) were communicated in-chat and tracked by PR
number in `docs/python_prd.md`'s changelog — not retroactively
persisted here.
