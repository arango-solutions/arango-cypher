/**
 * Tests for the chat composer Send-pipeline helpers (Phase 1).
 *
 * Pin the degradation contract (§3.2 of the workbench shell spec) and the
 * busy-flag -> stage derivation. Pure functions, so no DOM harness needed.
 */
import { describe, expect, it } from "vitest";

import {
  currentStage,
  isBusy,
  planSend,
  stageLabel,
  type PipelineFlags,
} from "./pipeline";

const FLAGS = (over: Partial<PipelineFlags> = {}): PipelineFlags => ({
  nlLoading: false,
  translating: false,
  executing: false,
  ...over,
});

describe("planSend", () => {
  it("always transpiles", () => {
    expect(planSend(true).translate).toBe(true);
    expect(planSend(false).translate).toBe(true);
  });

  it("executes only when connected", () => {
    expect(planSend(true).run).toBe(true);
    expect(planSend(false).run).toBe(false);
  });
});

describe("currentStage", () => {
  it("is idle when nothing runs", () => {
    expect(currentStage(FLAGS())).toBe("idle");
  });

  it("reports the in-order stage", () => {
    expect(currentStage(FLAGS({ nlLoading: true }))).toBe("generating");
    expect(currentStage(FLAGS({ translating: true }))).toBe("transpiling");
    expect(currentStage(FLAGS({ executing: true }))).toBe("running");
  });

  it("prefers the earliest active stage when several flags overlap", () => {
    expect(currentStage(FLAGS({ nlLoading: true, translating: true }))).toBe(
      "generating",
    );
    expect(currentStage(FLAGS({ translating: true, executing: true }))).toBe(
      "transpiling",
    );
  });
});

describe("stageLabel", () => {
  it("is empty for idle and non-empty otherwise", () => {
    expect(stageLabel("idle")).toBe("");
    expect(stageLabel("generating")).not.toBe("");
    expect(stageLabel("running")).not.toBe("");
  });
});

describe("isBusy", () => {
  it("is false only when no flag is set", () => {
    expect(isBusy(FLAGS())).toBe(false);
    expect(isBusy(FLAGS({ nlLoading: true }))).toBe(true);
    expect(isBusy(FLAGS({ executing: true }))).toBe(true);
  });
});
