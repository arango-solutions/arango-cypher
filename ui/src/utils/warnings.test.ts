/**
 * Tests for the AQL-pane warnings dismissal helpers.
 *
 * These guard the A.1 dedup work: translate/execute warnings live in a single
 * dismissible spot. The reset-on-change behavior is keyed off `warningsKey`,
 * and `filterVisibleWarnings` does the per-message hiding. Both are pure, so
 * they're tested here without a React/DOM harness.
 */
import { describe, expect, it } from "vitest";

import {
  filterVisibleWarnings,
  warningsKey,
  type TranslateWarning,
} from "./warnings";

describe("warningsKey", () => {
  it("is empty for no warnings", () => {
    expect(warningsKey([])).toBe("");
  });

  it("changes when the warning set changes", () => {
    const a: TranslateWarning[] = [{ message: "unlabeled MATCH" }];
    const b: TranslateWarning[] = [{ message: "unlabeled MATCH" }, { message: "other" }];
    expect(warningsKey(a)).not.toBe(warningsKey(b));
  });

  it("is stable for the same set", () => {
    const a: TranslateWarning[] = [{ message: "x" }, { message: "y" }];
    const b: TranslateWarning[] = [{ message: "x" }, { message: "y" }];
    expect(warningsKey(a)).toBe(warningsKey(b));
  });

  it("does not collide across differently-grouped messages", () => {
    // "a,b" vs "a","b" must not produce the same key.
    const joined: TranslateWarning[] = [{ message: "a,b" }];
    const split: TranslateWarning[] = [{ message: "a" }, { message: "b" }];
    expect(warningsKey(joined)).not.toBe(warningsKey(split));
  });
});

describe("filterVisibleWarnings", () => {
  const warnings: TranslateWarning[] = [
    { message: "unlabeled MATCH treated as side-store scan" },
    { message: "schema not fully warmed" },
  ];

  it("returns all when nothing is dismissed", () => {
    expect(filterVisibleWarnings(warnings, new Set())).toEqual(warnings);
  });

  it("hides a dismissed warning by message", () => {
    const dismissed = new Set(["unlabeled MATCH treated as side-store scan"]);
    const visible = filterVisibleWarnings(warnings, dismissed);
    expect(visible).toHaveLength(1);
    expect(visible[0].message).toBe("schema not fully warmed");
  });

  it("returns empty when all are dismissed", () => {
    const dismissed = new Set(warnings.map((w) => w.message));
    expect(filterVisibleWarnings(warnings, dismissed)).toEqual([]);
  });

  it("ignores dismissals that match no current warning", () => {
    const dismissed = new Set(["a stale message from a previous query"]);
    expect(filterVisibleWarnings(warnings, dismissed)).toEqual(warnings);
  });
});
